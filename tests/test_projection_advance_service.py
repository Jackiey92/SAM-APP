"""D12-4 real-chain orchestration, joint gates, and pointer crash recovery."""
import ast
from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app.facts_tree import projection_advance_service as service
from app.facts_tree import projection_router as router
from tests.test_projection_incremental import EXPECTED, _append, chain  # noqa: F401
from tests.test_projection_generation import TABLES, _read as read_standard
from tests.test_projection_derived_advance import _read as read_derived


STAMP = '2026-10-31T08:00:00+00:00'


def _run(repo, root, directory, target=None, **kwargs):
    return service.advance_projection(repo, root, directory, target_commit=target,
                                      now=lambda: STAMP, **{**EXPECTED, **kwargs})


def _current(root):
    return router.load_current(root, repo_id=EXPECTED['expected_repo_id'],
                               company_id=EXPECTED['expected_company_id'])


def _pointer(root):
    return router._scope(root, EXPECTED['expected_repo_id'], EXPECTED['expected_company_id'])


def _inventory(directory):
    return {p.name: (p.stat().st_ino, sha256(p.read_bytes()).hexdigest())
            for p in directory.iterdir()}


def _clean(root, directory):
    assert not any('.tmp-' in p.name or p.name.endswith(('-wal', '-shm', '-journal'))
                   for base in (root, directory) for p in base.rglob('*'))


def _full(repo, commit, directory):
    std = service.adv1.build_standard_generation_for_commit(repo, commit, directory, **EXPECTED)
    service.wide.build_projection_wide(std.db_path, directory=directory, **EXPECTED)
    derived = service.fts.build_projection_fts(std.db_path, directory=directory, **EXPECTED)
    return std, derived


def test_bootstrap_four_hops_joint_lock_and_every_stop_full_equivalence(chain, tmp_path, monkeypatch):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    counts = {'wide': 0, 'fts': 0, 'incremental': 0}
    for module, name, key in [(service.wide, 'build_projection_wide', 'wide'),
                              (service.fts, 'build_projection_fts', 'fts'),
                              (service.der, 'build_incremental_derived', 'incremental')]:
        original = getattr(module, name)
        def counted(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, counted)
    atomic = router._atomic_pointer
    seen = []

    def publish(path, pointer):
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        companion = service._derived_sibling(Path(pointer['db_path']))
        with closing(sqlite3.connect(companion.as_uri() + '?mode=ro', uri=True)) as conn:
            for table in ('wide_meta', 'fts_meta'):
                meta = dict(conn.execute('SELECT * FROM ' + table))
                assert meta['generation_id'] == pointer['generation_id']
                assert meta['projected_commit'] == pointer['projected_commit']
                assert meta['status'] == 'ready'
        atomic(path, pointer)
        seen.append(_current(root).projected_commit)

    monkeypatch.setattr(router, '_atomic_pointer', publish)
    # Omitted target exercises head_oid resolution.
    report = _run(repo, root, directory)
    assert report == service.AdvanceReport(None, commits[-1], commits, report.generations, False)
    assert seen == list(commits)
    assert counts == {'wide': 1, 'fts': 1, 'incremental': 3}
    assert len(report.generations) == 4
    assert _current(root).projected_commit == commits[-1]
    assert set(json.loads(_pointer(root).read_text())) == router._POINTER_KEYS
    monkeypatch.undo()
    for i, (commit, standard, derived) in enumerate(report.generations):
        assert Path(standard).is_absolute() and Path(derived).is_absolute()
        assert Path(derived) == service._derived_sibling(Path(standard))
        oracle, oracle_derived = _full(repo, commit, tmp_path / ('oracle-%d' % i))
        actual_standard = read_standard(standard)
        assert set(actual_standard) == TABLES
        assert actual_standard == read_standard(oracle.db_path)
        assert dict(actual_standard['generation_meta'])['generation_id'] == oracle.generation_id
        assert dict(actual_standard['generation_meta'])['render_sha256'] == oracle.render_sha256
        actual_derived = read_derived(derived)
        assert len(actual_derived['wide']) == 16
        assert actual_derived == read_derived(oracle_derived.db_path)
    assert [f.name for f in fields(report)] == [
        'from_commit', 'to_commit', 'published_commits', 'generations', 'already_current']
    with pytest.raises(FrozenInstanceError):
        report.to_commit = None
    _clean(root, directory)


def test_incremental_continuation_never_calls_full_derived_builders(chain, tmp_path, monkeypatch):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    _run(repo, root, directory, commits[1])
    before = _inventory(directory)
    incremental = service.der.build_incremental_derived
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail('full derived builder called on continuation')

    def spy(child, parent, **kwargs):
        calls.append((child, parent))
        return incremental(child, parent, **kwargs)

    monkeypatch.setattr(service.wide, 'build_projection_wide', forbidden)
    monkeypatch.setattr(service.fts, 'build_projection_fts', forbidden)
    monkeypatch.setattr(service.der, 'build_incremental_derived', spy)
    report = _run(repo, root, directory, commits[-1])
    assert report.from_commit == commits[1] and report.to_commit == commits[-1]
    assert report.published_commits == commits[2:]
    assert len(calls) == 2
    after = _inventory(directory)
    assert all(after[name] == value for name, value in before.items())
    assert _current(root).projected_commit == commits[-1]
    _clean(root, directory)


def test_already_current_has_zero_builds_or_publications(chain, tmp_path, monkeypatch):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    _run(repo, root, directory, commits[1])
    before, pointer = _inventory(directory), _pointer(root).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail('already-current must perform no build or publication')

    monkeypatch.setattr(service.adv1, 'build_standard_generation_for_commit', forbidden)
    monkeypatch.setattr(router, '_atomic_pointer', forbidden)
    assert _run(repo, root, directory, commits[1]) == service.AdvanceReport(
        commits[1], commits[1], (), (), True)
    assert _inventory(directory) == before and _pointer(root).read_bytes() == pointer


@pytest.mark.parametrize('phase', ['build', 'missing-derived'])
def test_failed_hop_stops_chain_preserves_watermark_and_recovers(chain, tmp_path, monkeypatch, phase):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    _run(repo, root, directory, commits[1])
    pointer = _pointer(root).read_bytes()
    incremental = service.der.build_incremental_derived
    calls = []

    def fail(child, parent, **kwargs):
        calls.append(child)
        if phase == 'build':
            raise service.der.DerivedAdvanceError('injected derived build failure')
        with closing(sqlite3.connect(Path(child).as_uri() + '?mode=ro', uri=True)) as conn:
            meta = dict(conn.execute('SELECT * FROM generation_meta'))
        return service.fts.FTSGeneration(str(directory / ('derived-' + meta['generation_id'] + '.sqlite')),
                                        meta['generation_id'], meta['projected_commit'],
                                        meta['render_sha256'], 0)

    monkeypatch.setattr(service.der, 'build_incremental_derived', fail)
    with pytest.raises(service.AdvanceServiceError) as error:
        _run(repo, root, directory, commits[-1])
    assert error.value.__cause__ is not None
    assert len(calls) == 1
    assert _pointer(root).read_bytes() == pointer
    assert _current(root).projected_commit == commits[1]
    monkeypatch.setattr(service.der, 'build_incremental_derived', incremental)
    recovered = _run(repo, root, directory, commits[-1])
    assert recovered.published_commits == commits[2:]
    _clean(root, directory)


@pytest.mark.parametrize('phase', ['before-replace', 'after-replace'])
def test_pointer_crash_before_or_after_second_hop_resumes_without_duplicates(
        chain, tmp_path, monkeypatch, phase):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    atomic = router._atomic_pointer
    calls = []

    def crash(path, pointer):
        calls.append(pointer['projected_commit'])
        if len(calls) == 2:
            if phase == 'after-replace':
                atomic(path, pointer)
            raise OSError('injected pointer crash ' + phase)
        atomic(path, pointer)

    monkeypatch.setattr(router, '_atomic_pointer', crash)
    with pytest.raises(service.AdvanceServiceError, match='injected pointer crash'):
        _run(repo, root, directory, commits[-1])
    stopped = 0 if phase == 'before-replace' else 1
    assert calls == list(commits[:2])
    assert _current(root).projected_commit == commits[stopped]
    before = _inventory(directory)
    resumed = []

    def record(path, pointer):
        resumed.append(pointer['projected_commit'])
        atomic(path, pointer)

    monkeypatch.setattr(router, '_atomic_pointer', record)
    result = _run(repo, root, directory, commits[-1])
    assert result.from_commit == commits[stopped]
    assert result.published_commits == commits[stopped + 1:]
    assert resumed == list(commits[stopped + 1:])
    after = _inventory(directory)
    assert all(after[name] == value for name, value in before.items())
    assert len(list(directory.glob('gen-*.sqlite'))) == 4
    assert len(list(directory.glob('derived-*.sqlite'))) == 4
    assert _current(root).projected_commit == commits[-1]
    assert _run(repo, root, directory, commits[-1]).already_current
    _clean(root, directory)


@pytest.mark.parametrize('failure', ['target-missing', 'current-not-ancestor', 'company',
                                     'generations-symlink', 'derived-symlink'])
def test_invalid_target_identity_or_symlink_leaves_current_unchanged(chain, tmp_path, failure):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    _run(repo, root, directory, commits[1])
    pointer = _pointer(root).read_bytes()
    target, expected = commits[-1], {}
    if failure == 'target-missing':
        target = 'f' * 40
    elif failure == 'current-not-ancestor':
        target = commits[0]
    elif failure == 'company':
        expected['expected_company_id'] = 'wrong'
    elif failure == 'generations-symlink':
        linked = tmp_path / 'linked'
        linked.symlink_to(directory, target_is_directory=True)
        directory = linked
    else:
        sibling = service._derived_sibling(Path(_current(root).db_path))
        moved = tmp_path / 'old-derived.sqlite'
        sibling.rename(moved)
        sibling.symlink_to(moved)
    with pytest.raises(service.AdvanceServiceError) as error:
        _run(repo, root, directory, target, **expected)
    assert error.value.__cause__ is not None
    assert _pointer(root).read_bytes() == pointer
    assert _current(root).projected_commit == commits[1]


@pytest.mark.parametrize('failure', ['wide-status', 'fts-status', 'wide-gid', 'fts-commit',
                                     'missing-wide-table', 'missing-fts-table', 'symlink',
                                     'result-identity', 'result-ready', 'standard-count'])
def test_joint_gate_rejects_bad_bundle_before_pointer_change(chain, tmp_path, failure):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    _run(repo, root, directory, commits[0])
    pointer = _pointer(root).read_bytes()
    std, derived = _full(repo, commits[1], directory)
    if failure == 'result-identity':
        derived = replace(derived, projected_commit=commits[0])
    elif failure == 'result-ready':
        derived = replace(derived, ready=False)
    elif failure == 'standard-count':
        std = replace(std, record_count=std.record_count + 1)
    elif failure == 'symlink':
        path = Path(derived.db_path)
        moved = tmp_path / 'derived.sqlite'
        path.rename(moved)
        path.symlink_to(moved)
    else:
        with closing(sqlite3.connect(derived.db_path)) as conn, conn:
            if failure in {'wide-status', 'fts-status'}:
                conn.execute("UPDATE " + failure.split('-')[0] + "_meta SET value='staging' WHERE key='status'")
            elif failure == 'wide-gid':
                conn.execute("UPDATE wide_meta SET value=? WHERE key='generation_id'", ('a' * 32,))
            elif failure == 'fts-commit':
                conn.execute("UPDATE fts_meta SET value=? WHERE key='projected_commit'", (commits[0],))
            elif failure == 'missing-wide-table':
                conn.execute('DROP TABLE dw_fact_current')
            else:
                conn.execute('DROP TABLE fts_records')
    with pytest.raises(service.AdvanceServiceError):
        service._publish_bundle(root, directory, std, derived, now=lambda: STAMP, **EXPECTED)
    assert _pointer(root).read_bytes() == pointer
    assert _current(root).projected_commit == commits[0]


def test_bundle_idempotent_publication_does_not_refresh_timestamp(chain, tmp_path, monkeypatch):
    repo, commits = chain
    root, directory = tmp_path / 'root', tmp_path / 'generations'
    _run(repo, root, directory, commits[0])
    std, derived = _full(repo, commits[0], directory)
    pointer = _pointer(root).read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail('identical identity must not refresh pointer or timestamp')

    monkeypatch.setattr(router, '_atomic_pointer', forbidden)
    serving = service._publish_bundle(root, directory, std, derived, now=forbidden, **EXPECTED)
    assert serving == _current(root)
    assert _pointer(root).read_bytes() == pointer


@pytest.mark.parametrize('path', ['ordinary.sqlite', 'derived-abc.sqlite', 'gen-abc.txt'])
def test_derived_sibling_rejects_nonstandard_filename(path):
    with pytest.raises(service.AdvanceServiceError):
        service._derived_sibling(Path(path))


def test_service_source_import_boundary():
    tree = ast.parse(Path(service.__file__).read_text())
    allowed_std = {'contextlib', 'dataclasses', 'datetime', 'fcntl', 'os', 'pathlib'}
    allowed_modules = {'projection_advance', 'projection_derived_advance', 'projection_router',
                       'projection_wide', 'projection_fts', 'projection_generation'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed_std for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            if node.module == 'app.facts_tree':
                assert {alias.name for alias in node.names} <= allowed_modules
            else:
                assert node.module in allowed_std
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'exec', 'eval', '__import__'}
