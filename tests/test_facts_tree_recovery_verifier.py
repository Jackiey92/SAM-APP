"""Real broker/freeze v2 packages, isolated copies, no mocked success paths."""
from __future__ import annotations

import ast
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from app.facts_tree import recovery_verifier as verifier
from app.facts_tree.backup_assembly import assemble_backup, verify_package_payload
from app.facts_tree.backup_manifest import build_final_manifest, build_verification_report
from tests.test_facts_tree_backup_assembly import world, _package_dir, _fingerprint


@pytest.fixture
def package(world, tmp_path):
    result = assemble_backup(world['make_plan'](), world['green_bid'])
    assert result.status == 'published', result
    source = _package_dir(world, world['green_bid'], world['barrier'])
    target = tmp_path / 'clean-room' / 'package'
    shutil.copytree(source, target)
    return target


def _manifest(package):
    return json.loads((package / 'manifest.json').read_bytes())


def _seal(package, core, *, version=2):
    report = build_verification_report(core)
    manifest = build_final_manifest(core, report)
    report['version'] = version
    if version != 2:
        from app.facts_tree.backup_manifest import compute_report_digest
        manifest['version'] = version
        manifest['verification']['report_digest'] = compute_report_digest(report)
    (package / 'report.json').write_text(json.dumps(report))
    (package / 'manifest.json').write_text(json.dumps(manifest))


def _refresh(package, core, path):
    data = (package / path).read_bytes()
    entry = next(e for e in core['payload']['files'] if e['path'] == path)
    old_size = entry['size_bytes']
    entry.update(sha256_digest='sha256:' + hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    core['payload']['totals']['total_bytes'] += len(data) - old_size
    if entry['family'] == 'w':
        anchor = next(e for e in core['databases'] if e['role'] == entry['role'])
        anchor.update(size_bytes=len(data), sha256_digest=entry['sha256_digest'])
        core['payload']['w']['total_bytes'] += len(data) - old_size
    elif entry['family'] == 'r2':
        core['payload']['r2'].update(ledger_file_sha256=entry['sha256_digest'], ledger_file_size=len(data))
    _seal(package, core)


def _refuse(package, reason):
    verdict = verifier.verify_recovery_package(package)
    assert not verdict.recoverable, asdict(verdict)
    assert reason in verdict.blockers, asdict(verdict)
    return verdict


def _a3_fingerprint(package):
    paths = [package] if package.is_file() else [package, *package.rglob('*')]
    return {str(p.relative_to(package)): (p.lstat().st_ino, p.lstat().st_mtime_ns,
            p.lstat().st_size, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in paths if not p.is_symlink()}


def test_real_clean_room_green_and_cross_check(package, world):
    before = _a3_fingerprint(package)
    assert verify_package_payload(package).self_contained
    verdict = verifier.verify_recovery_package(package)
    assert verdict.recoverable, (verdict.blockers, verdict.checks)
    assert verdict.package_format == 2 and verdict.self_contained
    assert all(check.passed for check in verdict.checks)
    core = _manifest(package)['core']
    assert verdict.identity == core['identity']
    assert verdict.bundle_head_oid == world['commit']
    assert verdict.closure_r_list_digest == core['closure']['r_list_digest']
    assert 'host_tables_absent' in verdict.advisories
    assert 'closure_replay_deferred' not in verdict.advisories
    assert {c.name for c in verdict.checks if c.passed} >= {'R.closure_digest', 'R.closure_bytes'}
    assert verdict.verified_at.endswith('Z')
    assert _a3_fingerprint(package) == before


def test_bundle_primitive(package):
    fd = os.open(package / 'authority.bundle', os.O_RDONLY)
    try:
        reasons = []
        try:
            verifier._bundle(fd, _manifest(package)['core'], reasons.append)
        except subprocess.CalledProcessError as exc:
            pytest.fail(exc.stderr.decode())
        assert reasons == []
    finally:
        os.close(fd)


def test_legacy_v1(package):
    core = _manifest(package)['core']
    core.pop('payload')
    shutil.rmtree(package / 'payload')
    _seal(package, core, version=1)
    verdict = _refuse(package, 'legacy_three_file')
    assert not verdict.self_contained and verdict.package_format == 1
    assert 'legacy_three_file_rebackup_required' in verdict.advisories
    assert not any(c.name in {'W', 'R', 'R2'} for c in verdict.checks)


def test_unsupported_envelope(package):
    manifest = _manifest(package)
    manifest['version'] = 3
    (package / 'manifest.json').write_text(json.dumps(manifest))
    _refuse(package, 'unsupported_envelope_version')


@pytest.mark.parametrize('name,reason', [
    ('manifest.json', 'missing_manifest_or_report'), ('report.json', 'missing_manifest_or_report'),
    ('authority.bundle', 'bundle_missing'), ('payload', 'payload_missing'),
    ('payload/w/w_items.sqlite', 'payload_inventory_mismatch'),
    ('payload/mapping-version-index.sqlite', 'r2_missing')])
def test_missing(package, name, reason):
    path = package / name
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    _refuse(package, reason)


@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink', 'fifo'])
def test_smuggled_entry(package, kind):
    path = package / 'payload' / 'extra'
    if kind == 'file':
        path.write_bytes(b'extra')
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'symlink':
        path.symlink_to('/etc/passwd')
    else:
        os.mkfifo(path)
    _refuse(package, 'payload_inventory_mismatch')


@pytest.mark.parametrize('target', ['manifest.json', 'authority.bundle', 'payload/w/w_items.sqlite'])
def test_symlink_replacement(package, target):
    path = package / target
    saved = package.parent / 'outside'
    path.rename(saved)
    path.symlink_to(saved)
    verdict = verifier.verify_recovery_package(package)
    assert not verdict.recoverable
    if target.startswith('payload'):
        assert 'payload_unsafe_path' in verdict.blockers


def test_symlink_ancestor_refused(package):
    linked = package.parent / 'alias'
    linked.symlink_to(package, target_is_directory=True)
    _refuse(linked, 'digest_chain')


def test_digest_chain_and_duplicate_json(package):
    manifest = _manifest(package)
    manifest['core']['backup_id'] = 'tampered'
    (package / 'manifest.json').write_text(json.dumps(manifest))
    _refuse(package, 'digest_chain')
    (package / 'manifest.json').write_text('{"version":2,"version":1}')
    _refuse(package, 'digest_chain')


@pytest.mark.parametrize('mode', ['missing', 'schema', 'files', 'duplicate', 'totals'])
def test_payload_malformed(package, mode):
    core = _manifest(package)['core']
    if mode == 'missing':
        core.pop('payload')
        reason = 'payload_missing'
    else:
        reason = 'payload_malformed'
        if mode == 'schema':
            core['payload']['schema'] = 'other'
        elif mode == 'files':
            core['payload']['files'] = None
        elif mode == 'duplicate':
            core['payload']['files'].append(core['payload']['files'][0])
        else:
            core['payload']['totals']['files'] += 1
            reason = 'payload_inventory_mismatch'
    _seal(package, core)
    _refuse(package, reason)


@pytest.mark.parametrize('mode', ['byte', 'schema', 'corrupt', 'foreign-key', 'role', 'file'])
def test_w_damage(package, mode):
    path = 'payload/w/w_items.sqlite'
    core = _manifest(package)['core']
    reason = 'w_byte_mismatch:w_items'
    if mode in {'byte', 'corrupt'}:
        data = bytearray((package / path).read_bytes())
        data[0] ^= 255
        (package / path).write_bytes(data)
        if mode == 'corrupt':
            _refresh(package, core, path)
            reason = 'w_corrupt:w_items'
    elif mode == 'schema':
        with sqlite3.connect(package / path) as db:
            db.execute('CREATE TABLE extra (id INTEGER)')
        _refresh(package, core, path)
        reason = 'w_schema_mismatch:w_items'
    elif mode == 'foreign-key':
        with sqlite3.connect(package / path) as db:
            db.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE child(id INTEGER REFERENCES parent(id))')
            db.execute('INSERT INTO child VALUES (1)')
        core['databases'][0]['schema']['tables'] += ['parent', 'child']
        _refresh(package, core, path)
        reason = 'w_corrupt:w_items'
    else:
        if mode == 'role':
            next(e for e in core['payload']['files'] if e['family'] == 'w')['role'] = '../escape'
            reason = 'payload_malformed'
        else:
            core['databases'][0]['file'] = 'other.sqlite'
        _seal(package, core)
    _refuse(package, reason)


@pytest.mark.parametrize('mode', ['byte', 'coordinate', 'link', 'count', 'digests', 'closure', 'status'])
def test_r_damage(package, mode):
    core = _manifest(package)['core']
    entry = next(e for e in core['payload']['files'] if e['family'] == 'r')
    path = package / entry['path']
    reason = 'r_byte_mismatch'
    if mode == 'byte':
        data = bytearray(path.read_bytes()); data[0] ^= 1; path.write_bytes(data)
    elif mode == 'coordinate':
        entry['path'] = 'payload/r/../../outside'
        reason = 'r_unsafe_coordinate'
    elif mode == 'link':
        path.unlink(); path.symlink_to('/etc/passwd')
        reason = 'r_unsafe_coordinate'
    elif mode in {'count', 'digests'}:
        if mode == 'count':
            core['payload']['r']['physical_files'] += 1
        else:
            core['payload']['r']['digests'].pop()
        reason = 'r_inventory_mismatch'
    elif mode == 'closure':
        core['payload']['r']['r_list_digest'] = 'sha256:' + '0' * 64
        reason = 'r_list_digest_mismatch'
    else:
        core['closure']['counts']['missing'] = 1
        reason = 'closure_not_publishable'
    _seal(package, core)
    _refuse(package, reason)


@pytest.mark.parametrize('mode', ['byte', 'row', 'digest', 'shape', 'scope', 'backing', 'skipped'])
def test_r2_damage(package, mode):
    path = 'payload/mapping-version-index.sqlite'
    core = _manifest(package)['core']
    reason = 'payload_r2_shape_mismatch'
    if mode == 'byte':
        with (package / path).open('ab') as stream:
            stream.write(b'bad')
        reason = 'r2_byte_mismatch'
    elif mode in {'row', 'scope'}:
        with sqlite3.connect(package / path) as db:
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                db.execute('DROP TRIGGER "' + name + '"')
            if mode == 'row':
                db.execute('UPDATE fact_mapping_version_registrations SET size_bytes=size_bytes+1')
            else:
                db.execute("UPDATE fact_mapping_version_registrations SET company_id='foreign'")
        _refresh(package, core, path)
        reason = 'r2_row_integrity'
    elif mode == 'digest':
        core['mapping_ledger']['ledger_digest'] = core['payload']['r2']['ledger_digest'] = 'sha256:' + '0' * 64
        reason = 'r2_ledger_digest_mismatch'
    elif mode == 'shape':
        core['payload']['r2']['sources'][0]['versions'] += 1
    elif mode == 'skipped':
        core['payload']['r2']['status'] = 'skipped'
    else:
        entry = next(e for e in core['payload']['files'] if e.get('storage_class') == 'mapping_objects')
        (package / entry['path']).unlink()
        reason = 'r2_backing_missing'
    _seal(package, core)
    _refuse(package, reason)


@pytest.mark.parametrize('mode', ['expected', 'missing', 'empty', 'scope'])
def test_identity(package, mode):
    core = _manifest(package)['core']
    if mode == 'expected':
        verdict = verifier.verify_recovery_package(package, expected_identity={'company_id': 'foreign'})
        assert not verdict.recoverable and 'identity_mismatch' in verdict.blockers
        return
    if mode == 'missing':
        del core['identity']['installation_id']
    elif mode == 'empty':
        core['identity']['company_id'] = ''
    else:
        core['identity']['scope'] = {'company_id': 'foreign'}
    _seal(package, core)
    _refuse(package, 'identity_mismatch')


@pytest.mark.parametrize('mode', ['byte', 'oid', 'length', 'pin', 'genesis', 'count'])
def test_bundle_damage(package, mode):
    core = _manifest(package)['core']
    reason = 'bundle_authority_mismatch'
    if mode == 'byte':
        (package / 'authority.bundle').write_bytes(b'broken')
        reason = 'bundle_byte_mismatch'
    elif mode == 'oid':
        core['authority']['commit_oid'] = '0' * 40
        reason = 'bundle_head_mismatch'
    elif mode == 'length':
        core['authority']['object_format'] = 'sha256'
        reason = 'bundle_head_mismatch'
    elif mode == 'pin':
        core['authority']['pin_ref'] = 'refs/heads/main'
    elif mode == 'genesis':
        core['authority']['genesis_oid'] = '0' * 40
    else:
        core['authority']['commit_count'] += 1
    _seal(package, core)
    _refuse(package, reason)


def test_zero_writes_including_sources_current_and_git(package, world):
    roots = [package, world['retention'], world['two_a'], world['ledger_root'], world['app_db']]
    before = [_a3_fingerprint(root) for root in roots]
    project = Path(__file__).resolve().parents[1]
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=project, capture_output=True, check=True).stdout
    for _ in range(2):
        assert verifier.verify_recovery_package(package).recoverable
    assert before == [_a3_fingerprint(root) for root in roots]
    assert subprocess.run(['git', 'status', '--porcelain'], cwd=project, capture_output=True, check=True).stdout == status


def test_package_on_real_read_only_mount(package):
    before = _a3_fingerprint(package)
    # Deliberately provide hostile inherited Git configuration/proxy inputs.
    env = dict(os.environ, GIT_DIR='/nonexistent', GIT_OBJECT_DIRECTORY='/nonexistent',
               GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.hooksPath',
               GIT_CONFIG_VALUE_0='/nonexistent', HTTPS_PROXY='http://invalid:9')
    code = '''
import json, os, sqlite3, sys
from dataclasses import asdict
from app.facts_tree.recovery_verifier import verify_recovery_package
p=sys.argv[1]
try:
    sqlite3.connect('file:'+p+'/payload/mapping-version-index.sqlite?mode=rw', uri=True).execute('CREATE TABLE rw_probe(id)')
except sqlite3.Error:
    pass
else:
    raise AssertionError('fixture must reject rw entry')
try:
    open(p+'/write-probe','wb')
except OSError as exc:
    assert exc.errno == 30, exc
else:
    raise AssertionError('not a real read-only mount')
def audit(event,args):
    if event == 'sqlite3.connect':
        assert 'mode=ro' in str(args[0]), args
sys.addaudithook(audit)
v=verify_recovery_package(p)
assert v.recoverable, asdict(v)
assert 'closure_replay_deferred' not in v.advisories
assert all(c.passed for c in v.checks if c.name.startswith('R.closure_'))
print(json.dumps(asdict(v)))
'''
    args = ['bwrap', '--die-with-parent', '--unshare-net', '--unshare-user',
            '--ro-bind', '/', '/', '--bind', os.environ['SAM_TEST_ROOT'], os.environ['SAM_TEST_ROOT'],
            '--ro-bind', str(package), str(package),
            '--proc', '/proc', '--dev', '/dev', '--', sys.executable, '-c', code, str(package)]
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['recoverable']
    assert _a3_fingerprint(package) == before


@pytest.fixture
def host_package(package, world):
    # Real host APIs issue credentials, prepare, approve/deny and reserve effects.
    from app.host_identity import HostIdentityService
    from app.host_authorization import HostAuthorizationService
    from app.host_task_credentials import HostTaskCredentialService
    from app.host_prepared_effects import HostPreparedEffectService
    from app.host_approval_decisions import HostApprovalDecisionService, _SCHEMA as decision_schema
    from app.host_approval_reservations import HostApprovalReservationService
    from tests.test_host_prepared_effects import at, scope_kwargs, effect_kwargs
    from tests.test_host_approval_decisions import decision_kwargs
    from tests.test_host_approval_reservations import reserve_kwargs

    from decimal import Decimal
    core = _manifest(package)['core']
    source = sqlite3.connect(world['tmp'] / 'host-source.sqlite')
    identity = HostIdentityService(source)
    identity.initialize_installation(installation_id=core['identity']['installation_id'],
        company_id=core['identity']['company_id'], owner_principal_id='owner-r4', owner_role='owner', now=at(0))
    auth = HostAuthorizationService(source, identity)
    credentials = HostTaskCredentialService(source, identity, auth)
    effects = HostPreparedEffectService(source, identity, auth, credentials)
    decisions = HostApprovalDecisionService(source, identity, auth, credentials, effects)
    reservations = HostApprovalReservationService(source, identity, auth, credentials, effects, decisions)
    ticket = identity.issue_pairing_credential(now=at(10), lifetime_seconds=600)
    grant = identity.pair(ticket.secret, now=at(20), session_lifetime_seconds=3600)
    policy = auth.create_policy(grant.secret, now=at(30), **scope_kwargs())
    active = credentials.issue_task_credential(grant.secret, now=at(40), policy_id=policy.policy_id, expected_revision=1)
    revoked = credentials.issue_task_credential(grant.secret, now=at(41), policy_id=policy.policy_id, expected_revision=1)
    credentials.revoke_task_credential(grant.secret, now=at(42), task_id=revoked.task_id)
    ops = {}
    for index, name in enumerate(('approved', 'denied', 'pending', 'unknown')):
        effect = effects.prepare_effect(active.secret, now=at(50 + index), **effect_kwargs(idempotency_key=name, effect_manifest={'amount': Decimal('12345678901234567890.0100')}))
        ops[name] = effect.operation_id
        if name != 'unknown':
            decision = decisions.approve_effect(grant.secret, now=at(60 + index),
                **decision_kwargs(effect, decision='denied' if name == 'denied' else 'approved'))
            if name == 'pending':
                reservations.reserve_approval(active.secret, now=at(70), **reserve_kwargs(effect, decision))
    path = 'payload/w/w_items.sqlite'
    (package / path).unlink()
    target = sqlite3.connect(package / path)
    source.backup(target)
    source.close()
    # A reservation with no terminal decision in this snapshot is manual work.
    for name, in target.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='host_approval_decisions'").fetchall():
        target.execute('DROP TRIGGER "' + name + '"')
    target.execute('DELETE FROM host_approval_decisions WHERE operation_id=?', (ops['pending'],))
    target.commit()
    target.executescript(decision_schema)
    core['databases'][0]['schema']['tables'] = [r[0] for r in target.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    target.close()
    _refresh(package, core, path)
    return package, ops, active.task_id, revoked.task_id


def test_host_real_history_unknown_is_blocked_and_never_replayed(host_package):
    package, ops, active, revoked = host_package
    before = _fingerprint(package)
    verdict = _refuse(package, 'host_unknown_operation:' + ops['unknown'])
    assert verdict.blockers == ('host_unknown_operation:' + ops['unknown'],)
    assert 'host_unknown_operation:' + ops['unknown'] in verdict.advisories
    assert 'host_decided_operation:' + ops['approved'] + ':approved' in verdict.advisories
    assert 'host_decided_operation:' + ops['denied'] + ':denied' in verdict.advisories
    assert 'host_pending_operation:' + ops['pending'] in verdict.advisories
    assert 'credential_revoked:' + revoked in verdict.advisories
    assert 'credential_revoked:' + active not in verdict.advisories
    assert 'credential_rebind_required:' + active in verdict.advisories
    assert _fingerprint(package) == before


@pytest.mark.parametrize('mode', ['guard', 'row', 'index', 'credential', 'watermark', 'credential-table'])
def test_host_damage(host_package, mode):
    package, ops, active, _ = host_package
    path = 'payload/w/w_items.sqlite'
    core = _manifest(package)['core']
    with sqlite3.connect(package / path) as db:
        if mode == 'guard':
            db.execute('DROP TRIGGER host_prepared_effects_no_delete')
            reason = 'host_guard_missing:host_prepared_effects'
        elif mode in {'row', 'index'}:
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='host_approval_decisions'").fetchall():
                db.execute('DROP TRIGGER "' + name + '"')
            column = 'row_sha256' if mode == 'row' else 'task_id'
            db.execute('UPDATE host_approval_decisions SET ' + column + "='corrupted'")
            reason = 'host_row_integrity:host_approval_decisions'
        elif mode == 'credential-table':
            db.execute('DROP TABLE host_task_credential_events')
            core['databases'][0]['schema']['tables'].remove('host_task_credential_events')
            reason = 'credential_state_unknown:' + active
        else:
            table = 'host_task_credential_events' if mode == 'credential' else 'host_task_credentials_ledger_state'
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall():
                db.execute('DROP TRIGGER "' + name + '"')
            if mode == 'credential':
                db.execute("DELETE FROM host_task_credential_events WHERE task_id=?", (active,))
            else:
                db.execute('UPDATE host_task_credentials_ledger_state SET revocation_seq=99')
            reason = 'credential_state_unknown:' + active
    _refresh(package, core, path)
    _refuse(package, reason)


def test_r_authoritative_closure_digest_is_forwarded_not_replayed(package):
    core = _manifest(package)['core']
    # Explicit old v2 package: it has no canonical closure record to replay.
    del core['closure']['canonical_record']
    digest = 'sha256:' + '7' * 64
    core['closure']['r_list_digest'] = core['payload']['r']['r_list_digest'] = digest
    _seal(package, core)
    verdict = verifier.verify_recovery_package(package)
    assert verdict.recoverable, verdict.blockers
    assert verdict.closure_r_list_digest == digest
    assert 'closure_replay_deferred' in verdict.advisories


def test_r2_backing_only_and_no_ledger_installation(world, tmp_path):
    for with_ledger in (True, False):
        bid = 'bkp-r4-backing' if with_ledger else 'bkp-r4-no-ledger'
        frozen = world['freeze'](bid)
        plan = world['make_plan'](bid=bid, refs=(), version_root=world['ledger_root'] if with_ledger else None)
        assert assemble_backup(plan, bid).status == 'published'
        target = tmp_path / bid
        shutil.copytree(_package_dir(world, bid, frozen.barrier_id), target)
        verdict = verifier.verify_recovery_package(target)
        assert verdict.recoverable, verdict.blockers
        assert 'closure_replay_deferred' not in verdict.advisories
        assert {c.name for c in verdict.checks if c.passed} >= {'R.closure_digest', 'R.closure_bytes'}
        core = _manifest(target)['core']
        assert core['payload']['r']['physical_files'] == 0
        assert core['payload']['r2']['status'] == ('ok' if with_ledger else 'skipped')
        if with_ledger:
            assert core['payload']['r2']['backing_files'] == 3


@pytest.mark.parametrize('with_record', [False, True])
def test_sha256_manual_h1_authority_with_real_v2_payload(package, tmp_path, with_record):
    # Assembly's production closure call still defaults to sha1 (outside R4).
    # Replace H only with a real H1 SHA-256 authority; W/R/R2 stay real A2 bytes.
    from app.facts_tree.git_repository import FactsGitRepository
    core = _manifest(package)['core']
    if not with_record:
        del core['closure']['canonical_record']  # historical hand-composed v2 package
    repo = FactsGitRepository.initialize(tmp_path / 'sha256.git', object_format='sha256', **core['identity'])
    oid = repo.head_oid()
    repo.create_maintenance_pin(core['backup_id'], core['barrier_id'], oid)
    output = tmp_path / 'sha256.bundle'
    result = repo.export_maintenance_pin_bundle(core['backup_id'], core['barrier_id'], output)
    shutil.copyfile(output, package / 'authority.bundle')
    core['authority'].update(commit_oid=oid, genesis_oid=oid, object_format='sha256', commit_count=1)
    core['bundle'].update(size_bytes=result.size_bytes, sha256_digest='sha256:' + result.sha256_digest)
    _seal(package, core)
    verdict = verifier.verify_recovery_package(package)
    if with_record:
        assert not verdict.recoverable and 'identity_mismatch' in verdict.blockers
        return
    assert verdict.recoverable, verdict.blockers
    assert verdict.object_format == 'sha256' and len(verdict.bundle_head_oid) == 64


def test_import_surface_and_read_only_ast():
    source = Path(verifier.__file__).read_text()
    tree = ast.parse(source)
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imports == {'__future__', 'dataclasses', 'hashlib', 'json', 'os', 'sqlite3',
        'stat', 'subprocess', 'tempfile', 'typing', 'app.facts_tree.backup_manifest',
        'app.facts_tree.backup_assembly', 'app.facts_tree.backup_object_closure',
        'app.facts_tree.mapping_version_index'}
    ledger = next(n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                  and n.module == 'app.facts_tree.mapping_version_index')
    assert {a.name for a in ledger.names} == {'_row_digest', '_COLUMNS', '_DB_FILENAME',
        'SCHEMA', 'SCHEMA_VERSION', 'OBJECT_TYPE_MAPPING_VERSION', 'STORAGE_CLASS_MAPPING_OBJECTS', 'DIGEST_ALGORITHM'}
    allowed_app = {
        'app.facts_tree.backup_manifest': {'compute_core_digest', 'verify_final_manifest'},
        'app.facts_tree.backup_assembly': {'ASSEMBLY_CORE_SCHEMA', 'BUNDLE_NAME', 'MANIFEST_NAME', 'REPORT_NAME',
            'PAYLOAD_SCHEMA', 'PAYLOAD_VERSION', '_SAFE_ROLE_RE', '_payload_probe',
            '_replay_closure_digest', '_replay_closure_bytes'},
        'app.facts_tree.backup_object_closure': {'_clean_relative_coordinate'},
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module in allowed_app:
            assert {a.name for a in n.names} == allowed_app[n.module]
    forbidden = {'MappingVersionLedger', '__import__', 'eval', 'exec', 'compile',
                 'remove', 'unlink', 'rmdir', 'mkdir', 'rename', 'replace', 'system',
                 'popen', 'Popen', 'socket', 'connect_ex', 'assemble_backup', 'verify_package_payload'}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names | attrs) & (forbidden - {'replace'})
    assert not any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                   and n.value.id == 'os' and n.attr == 'replace' for n in ast.walk(tree))
    assert not any(isinstance(n, ast.keyword) and n.arg == 'shell' for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Attribute) and n.attr in {'O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_TRUNC', 'O_APPEND'} for n in ast.walk(tree))
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == 'subprocess']
    assert len(runs) == 1 and runs[0].func.attr == 'run'
    assert verifier._GIT_COMMANDS == frozenset({('init', '--bare'), ('bundle', 'verify'),
        ('bundle', 'list-heads'), ('bundle', 'unbundle'), ('rev-parse',), ('rev-list',),
        ('cat-file',), ('ls-tree',), ('fsck',)})
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_git']
    for call in calls:
        assert isinstance(call.args[0], ast.List)
        head = call.args[0].elts[0].value
        assert head in {'init', 'bundle', 'cat-file', 'fsck', 'rev-list'}
    assert 'TemporaryDirectory(prefix="sam-recovery-git-") as scratch' in source
    assert 'assert os.path.commonpath((scratch, bare)) == scratch and bare != scratch' in source
    assert '"--template="' in source and 'GIT_CONFIG_NOSYSTEM="1"' in source
    assert 'mode=ro&immutable=1' in source
    assert 'closure_replay_deferred' in verifier.__doc__


@pytest.mark.parametrize('command', ['clone', 'fetch', 'push', 'remote', 'config', 'gc', 'shell'])
def test_git_rejects_non_whitelisted_commands(tmp_path, command):
    with pytest.raises(AssertionError):
        verifier._git([command, 'https://invalid.example'], str(tmp_path))


@pytest.mark.parametrize('move_role', [False, True])
@pytest.mark.parametrize('foreign_scope', [False, True])
def test_host_in_flight_real_operation_and_role_discovery(host_package, move_role, foreign_scope):
    from app.facts_tree.operation_work_store import OperationWorkStore
    package, ops, active, _ = host_package
    core = _manifest(package)['core']
    path = 'payload/w/w_items.sqlite'
    with sqlite3.connect(package / path) as db:
        store = OperationWorkStore(db)
        db.execute('BEGIN IMMEDIATE')
        store.record_operation(core['identity']['repo_id'], 'foreign-company' if foreign_scope else core['identity']['company_id'],
            ops['unknown'], active, 'known-in-flight', '2026-11-02T00:00:00Z', {'source': 'r4'})
    with sqlite3.connect(package / path) as db:
        core['databases'][0]['schema']['tables'] = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    _refresh(package, core, path)
    if move_role:
        left, right = package / path, package / 'payload/w/w_ledger.sqlite'
        a, b = left.read_bytes(), right.read_bytes()
        left.write_bytes(b); right.write_bytes(a)
        one, two = core['databases']
        one['schema'], two['schema'] = two['schema'], one['schema']
        _refresh(package, core, path)
        _refresh(package, core, 'payload/w/w_ledger.sqlite')
    verdict = verifier.verify_recovery_package(package)
    if foreign_scope:
        assert not verdict.recoverable and 'host_unknown_operation:' + ops['unknown'] in verdict.blockers
    else:
        assert verdict.recoverable, verdict.blockers
        assert not any(reason.startswith('host_unknown_operation:') for reason in verdict.advisories)
    assert 'credential_rebind_required:' + active in verdict.advisories


@pytest.mark.parametrize('value', [None, [], {}, {'identity': []}, {'authority': []}])
def test_malformed_core_never_escapes(package, value):
    if not isinstance(value, dict):
        manifest = _manifest(package)
        manifest['core'] = value
        (package / 'manifest.json').write_text(json.dumps(manifest))
    else:
        _seal(package, value)
    verdict = verifier.verify_recovery_package(package)
    assert not verdict.recoverable and verdict.blockers


@pytest.mark.parametrize('args', [
    ['bundle', 'verify', '/etc/passwd'], ['init', '--bare', '/tmp/forbidden'],
    ['fsck', '--full', '--no-reflogs', '--config=bad'],
    ['cat-file', 'blob', '--batch-command'], ['rev-list', '--all'],
])
def test_git_rejects_unapproved_argument_shapes(tmp_path, args):
    with pytest.raises(AssertionError):
        verifier._git(args, str(tmp_path))


def test_host_missing_credential_table_requires_rebind(host_package):
    package, _ops, active, _ = host_package
    path = 'payload/w/w_items.sqlite'
    core = _manifest(package)['core']
    with sqlite3.connect(package / path) as db:
        db.execute('DROP TABLE host_task_credentials')
    core['databases'][0]['schema']['tables'].remove('host_task_credentials')
    _refresh(package, core, path)
    _refuse(package, 'credential_state_unknown:' + active)


# A3 revB section 5: mutations operate on real clean-room packages. Re-sealing
# is intentional: these tests distinguish the digest chain from independent replay.
def _a3_reseal(package, core):
    digest = verifier.compute_core_digest(core['closure']['canonical_record'])
    core['closure']['r_list_digest'] = core['payload']['r']['r_list_digest'] = digest
    _seal(package, core)


def _a3_check(package, name, reason):
    verdict = _refuse(package, reason)
    check = next(c for c in verdict.checks if c.name == name)
    assert not check.passed and reason in check.reasons, asdict(verdict)
    assert not verdict.self_contained
    return verdict


def test_a3_01_record_edit_without_chain(package):
    manifest = _manifest(package)
    manifest['core']['closure']['canonical_record']['entries'][0]['version'] = 'changed'
    (package / 'manifest.json').write_text(json.dumps(manifest))
    _refuse(package, 'digest_chain')


def test_a3_02_landed_byte_replay_independent_of_inventory(package):
    core = _manifest(package)['core']
    entry = next(e for e in core['payload']['files'] if e['family'] == 'r')
    path = package / entry['path']
    path.write_bytes(b'X' + path.read_bytes()[1:])
    _a3_check(package, 'R.closure_bytes', 'r_byte_mismatch')


def test_a3_03_record_digest_does_not_name_actual_bytes(package):
    core = _manifest(package)['core']
    core['closure']['canonical_record']['entries'][0]['sha256_digest'] = 'sha256:' + '0' * 64
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_bytes', 'r_byte_mismatch')


@pytest.mark.parametrize('mode', ['extra-file', 'missing-file', 'logical-ids', 'physical-count', 'logical-count', 'digests'])
def test_a3_04_05_layered_inventory(package, mode):
    core = _manifest(package)['core']
    record = core['closure']['canonical_record']
    if mode == 'extra-file':
        # Keep a valid family=r inventory file, omit its logical declaration.
        record['entries'].pop(0)
    elif mode == 'missing-file':
        entry = dict(record['entries'][0], logical_id='extra-present', sha256_digest='sha256:' + '0' * 64)
        record['entries'].append(entry)
    elif mode == 'logical-ids':
        next(e for e in core['payload']['files'] if e['family'] == 'r')['logical_ids'] = ['undeclared']
    elif mode == 'digests':
        core['payload']['r']['digests'] = ['sha256:' + '0' * 64]
    else:
        core['payload']['r']['physical_files' if mode == 'physical-count' else 'present_entries'] += 1
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_bytes', 'r_inventory_mismatch')


@pytest.mark.parametrize('mode', ['bare', 'double', 'upper', 'newline', 'space'])
def test_a3_06_strict_digest(package, mode):
    core = _manifest(package)['core']
    entry = core['closure']['canonical_record']['entries'][0]
    digest = entry['sha256_digest']
    entry['sha256_digest'] = {'bare': digest[7:], 'double': 'sha256:' + digest,
        'upper': digest.upper(), 'newline': digest + '\n', 'space': ' ' + digest}[mode]
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest', 'closure_record_malformed')


@pytest.mark.parametrize('mode', ['shared', 'duplicate-id', 'size-conflict'])
def test_a3_07_shared_physical_digest(package, mode):
    core = _manifest(package)['core']
    record = core['closure']['canonical_record']
    entry = dict(record['entries'][0])
    entry['logical_id'] = 'shared-second-id' if mode != 'duplicate-id' else entry['logical_id']
    if mode == 'size-conflict':
        entry['size_bytes'] += 1
    record['entries'].append(entry)
    physical = next(e for e in core['payload']['files'] if e['family'] == 'r'
                    and e['sha256_digest'] == entry['sha256_digest'])
    physical['logical_ids'].append(entry['logical_id'])
    core['closure']['counts']['present'] += 1
    core['payload']['r']['present_entries'] += 1
    core['payload']['r']['by_class'][entry['storage_class']]['entries'] += 1
    _a3_reseal(package, core)
    if mode == 'shared':
        verdict = verifier.verify_recovery_package(package)
        assert verdict.recoverable, asdict(verdict)
        assert core['payload']['r']['present_entries'] > core['payload']['r']['physical_files']
        assert 'closure_replay_deferred' not in verdict.advisories
    else:
        _a3_check(package, 'R.closure_bytes', 'r_inventory_mismatch')


@pytest.mark.parametrize('record', [None, {}, [], False, 'bad', 1])
def test_a3_09_present_malformed_never_legacy(package, record):
    core = _manifest(package)['core']
    core['closure']['canonical_record'] = record
    _seal(package, core)
    _a3_check(package, 'R.closure_digest', 'closure_record_malformed')


@pytest.mark.parametrize('field,value', [('schema', 'wrong'), ('version', 2), ('version', True),
    ('entries', {}), ('scope_violations', None)])
def test_a3_09_record_shape(package, field, value):
    core = _manifest(package)['core']
    core['closure']['canonical_record'][field] = value
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest', 'closure_record_malformed')


@pytest.mark.parametrize('value', [-1, 1.5, '12', True, None])
def test_a3_09_size_types(package, value):
    core = _manifest(package)['core']
    core['closure']['canonical_record']['entries'][0]['size_bytes'] = value
    if isinstance(value, float):
        # Canonical serialization itself refuses floats; raw hostile JSON
        # must still report malformed, without leaking a parser exception.
        manifest = _manifest(package)
        manifest['core'] = core
        (package / 'manifest.json').write_text(json.dumps(manifest))
        _refuse(package, 'closure_record_malformed')
        return
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest', 'closure_record_malformed')


def test_a3_09_duplicate_json_key(package):
    path = package / 'manifest.json'
    data = path.read_text()
    assert '"canonical_record"' in data
    path.write_text(data.replace('"canonical_record": {', '"canonical_record": null, "canonical_record": {', 1))
    _refuse(package, 'closure_record_malformed')


@pytest.mark.parametrize('mode', ['authorized-installation', 'authorized-company', 'entry-installation',
    'entry-company', 'commit', 'commit-length'])
def test_a3_10_identity_and_commit(package, mode):
    core = _manifest(package)['core']
    record = core['closure']['canonical_record']
    if mode.startswith('authorized'):
        record['authorized_scope']['installation_id' if mode.endswith('installation') else 'company_id'] = 'foreign'
    elif mode.startswith('entry'):
        entry = next(e for e in record['entries'] if e['scope']['kind'] == 'company')
        entry['scope']['installation_id' if mode.endswith('installation') else 'company_id'] = 'foreign'
    else:
        record['commit_oid'] = '0' * (40 if mode == 'commit' else 64)
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest', 'identity_mismatch')


@pytest.mark.parametrize('status', ['missing', 'corrupt', 'unsafe_path', 'out_of_scope',
    'referenced_but_not_externalized', 'unknown'])
def test_a3_11_no_filtering_bad_status(package, status):
    core = _manifest(package)['core']
    core['closure']['canonical_record']['entries'][0]['status'] = status
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest',
              'closure_record_malformed' if status == 'unknown' else 'closure_not_publishable')


@pytest.mark.parametrize('path', ['/absolute', '../escape', 'a/../b', 'a//b', 'a\\b', 'a\x00b', ''])
def test_a3_12_source_coordinate_lexical_only(package, path):
    core = _manifest(package)['core']
    core['closure']['canonical_record']['entries'][0]['relative_path'] = path
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_bytes', 'r_unsafe_coordinate')


def test_a3_12_old_source_layout_is_never_opened(package):
    core = _manifest(package)['core']
    core['closure']['canonical_record']['entries'][0]['relative_path'] = 'legacy/explicit/source-object'
    _a3_reseal(package, core)
    verdict = verifier.verify_recovery_package(package)
    assert verdict.recoverable, asdict(verdict)


@pytest.mark.parametrize('mode', ['middle-link', 'final-link', 'fifo'])
def test_a3_12_physical_coordinate_pin(package, mode, tmp_path):
    core = _manifest(package)['core']
    item = next(e for e in core['payload']['files'] if e['family'] == 'r')
    path = package / item['path']
    if mode == 'middle-link':
        outside = tmp_path / 'outside-shard'
        path.parent.rename(outside)
        path.parent.symlink_to(outside, target_is_directory=True)
    else:
        path.unlink()
        if mode == 'final-link':
            outside = tmp_path / 'outside-file'
            outside.write_bytes(b'outside')
            path.symlink_to(outside)
        else:
            os.mkfifo(path)
    _a3_check(package, 'R.closure_bytes', 'r_byte_mismatch')


def test_a3_13_json_roundtrip_and_key_order(package):
    core = _manifest(package)['core']
    record = core['closure']['canonical_record']
    original = verifier.compute_core_digest(record)
    def reverse(value):
        if isinstance(value, dict):
            return {k: reverse(v) for k, v in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reverse(v) for v in value]
        return value
    record = json.loads(json.dumps(reverse(record)))
    assert verifier.compute_core_digest(record) == original
    core['closure']['canonical_record'] = record
    _seal(package, core)
    assert verifier.verify_recovery_package(package).recoverable


def test_a3_16_new_package_cannot_forward_forged_closure_digest(package):
    core = _manifest(package)['core']
    core['closure']['r_list_digest'] = core['payload']['r']['r_list_digest'] = 'sha256:' + '7' * 64
    _seal(package, core)
    _a3_check(package, 'R.closure_digest', 'r_list_digest_mismatch')


def test_a3_pure_rules_have_no_store_or_write_surface():
    from app.facts_tree import backup_assembly as assembly
    tree = ast.parse(Path(assembly.__file__).read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'_closure_record', '_replay_closure_digest', '_replay_closure_bytes'}]
    assert len(functions) == 3
    calls = {n.func.id for f in functions for n in ast.walk(f)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {'collect_object_closure', 'ClosureStores', 'MappingVersionLedger',
                        'ArchiveFileStore', 'VersionedMappingObjectStore', 'open'}
    attrs = {n.attr for f in functions for n in ast.walk(f) if isinstance(n, ast.Attribute)}
    assert not attrs & {'open', 'connect', 'mkdir', 'write', 'rename', 'unlink', 'run'}


@pytest.mark.parametrize('field,value', [
    ('logical_id', ''), ('object_type', 'unknown'), ('version', None),
    ('scope', None), ('scope', {}), ('referenced_by', []), ('referenced_by', [None]),
    ('referenced_by', [{'kind': 'X', 'root_id': 'id', 'coordinate': 'c'}]),
    ('storage_class', 'unknown'), ('digest_algorithm', 'sha1'), ('issue', 'bad'),
])
def test_a3_09_entry_structure(package, field, value):
    core = _manifest(package)['core']
    core['closure']['canonical_record']['entries'][0][field] = value
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest', 'closure_record_malformed')


def test_a3_scope_violation_never_publishable(package):
    core = _manifest(package)['core']
    record = core['closure']['canonical_record']
    entry = record['entries'][0]
    record['scope_violations'] = [{k: entry[k] for k in (
        'logical_id', 'object_type', 'sha256_digest', 'version', 'key_reference', 'scope', 'referenced_by')}]
    record['scope_violations'][0]['reason'] = 'foreign_company'
    _a3_reseal(package, core)
    _a3_check(package, 'R.closure_digest', 'closure_not_publishable')
