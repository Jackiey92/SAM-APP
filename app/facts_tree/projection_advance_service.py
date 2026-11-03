"""Commit-watermark orchestration with jointly gated standard/derived publication.

No application/query wiring, event bus, event-ID deduplication, vectors, retire,
undo, real-company migration or remote operations are implemented here.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path

from app.facts_tree import projection_advance as adv1
from app.facts_tree import projection_derived_advance as der
from app.facts_tree import projection_router as router
from app.facts_tree import projection_wide as wide
from app.facts_tree import projection_fts as fts
from app.facts_tree import projection_generation as gen


@dataclass(frozen=True)
class AdvanceReport:
    from_commit: str | None
    to_commit: str | None
    published_commits: tuple[str, ...]
    generations: tuple[tuple[str, str, str], ...]
    already_current: bool


class AdvanceServiceError(ValueError):
    """Failed advance evaluation, construction or joint publication."""


def _derived_sibling(std_path):
    """Derive the companion filename without touching the filesystem."""
    path = Path(std_path)
    if not path.name.startswith('gen-') or not path.name.endswith('.sqlite'):
        raise AdvanceServiceError('standard generation filename must be gen-*.sqlite')
    return path.parent / ('derived-' + path.name[len('gen-'):])


def _publish_bundle(root, generations_dir, built_gen: gen.BuiltProjectionGeneration,
                    derived: fts.FTSGeneration, *, expected_repo_id,
                    expected_company_id, now=None) -> router.ServingGeneration:
    """Recheck both immutable files under the router lock, then select once."""
    lock_fd = None
    try:
        if type(built_gen) is not gen.BuiltProjectionGeneration or type(derived) is not fts.FTSGeneration:
            raise TypeError('bundle must contain built standard and FTS generations')
        if built_gen.ready is not True or derived.ready is not True:
            raise ValueError('both bundle components must be ready')
        if now is not None and not callable(now):
            raise TypeError('now must be callable')
        pointer_path = router._scope(root, expected_repo_id, expected_company_id)
        directory = router._path(generations_dir, 'generations_dir')

        def paths():
            router._no_symlinks(directory)
            parent = directory.resolve(strict=True)
            standard = router._regular(Path(built_gen.db_path))
            companion = router._regular(Path(derived.db_path))
            router._within(standard, parent)
            router._within(companion, parent)
            if (not parent.is_dir() or standard.parent != parent or companion.parent != parent
                    or standard.name != 'gen-' + built_gen.generation_id + '.sqlite'
                    or companion.name != 'derived-' + derived.generation_id + '.sqlite'
                    or companion != _derived_sibling(standard)
                    or built_gen.generation_id != derived.generation_id
                    or built_gen.projected_commit != derived.projected_commit
                    or built_gen.render_sha256 != derived.render_sha256):
                raise ValueError('bundle filename/directory/identity mismatch')
            return standard, companion

        paths()
        root_path = router._path(root, 'root')
        router._no_symlinks(root_path)
        root_path.mkdir(parents=True, exist_ok=True)
        router._no_symlinks(root_path)
        lock_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        standard, companion = paths()
        with closing(router._connect(standard)) as connection:
            serving = router._verify(connection, standard, expected_repo_id, expected_company_id)
        if (serving.generation_id != built_gen.generation_id
                or serving.projected_commit != built_gen.projected_commit
                or serving.render_sha256 != built_gen.render_sha256
                or serving.record_count != built_gen.record_count):
            raise ValueError('built identity/count does not match generation_meta')
        with closing(router._connect(companion)) as connection:
            connection.execute('BEGIN')
            for table in ('wide_meta', 'fts_meta'):
                meta = dict(connection.execute('SELECT key,value FROM ' + table))
                if (meta.get('generation_id') != serving.generation_id
                        or meta.get('projected_commit') != serving.projected_commit
                        or meta.get('status') != 'ready'):
                    raise ValueError('derived component not jointly ready: ' + table)
            required = {'fts_records', *('dw_' + family for family in wide.SCHEMA_COLUMNS)}
            if not required <= wide._table_names(connection):
                raise ValueError('derived required tables missing')
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('derived integrity_check failed')
        identity = router._identity(serving)
        try:
            old = router._pointer(pointer_path, expected_repo_id, expected_company_id)
        except router.NoCurrentProjection:
            old = None
        if old is not None and {key: old[key] for key in identity} == identity:
            return serving
        stamp = now() if now is not None else datetime.now(timezone.utc).isoformat()
        router._timestamp(stamp)
        router._atomic_pointer(pointer_path, {**identity, 'published_at': stamp})
        return serving
    except Exception as exc:
        raise AdvanceServiceError('cannot jointly publish projection bundle: ' + str(exc)) from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


publish_projection_bundle = _publish_bundle


def advance_projection(repository, root, generations_dir, *, expected_repo_id,
                       expected_company_id, target_commit: str | None = None,
                       now=None) -> AdvanceReport:
    """Advance one authoritative commit at a time; current is the sole watermark."""
    try:
        target = target_commit or repository.head_oid()
        chain = repository.list_commit_chain(repository.zero_oid, target)
        try:
            cur = router.load_current(root, repo_id=expected_repo_id, company_id=expected_company_id)
        except router.NoCurrentProjection:
            cur = None
        current_commit = cur.projected_commit if cur else None
        win = adv1.evaluate_advance_window(current_commit, target, chain)
        if win.is_up_to_date:
            return AdvanceReport(current_commit, current_commit, (), (), True)
        directory = router._path(generations_dir, 'generations_dir')
        router._no_symlinks(directory)
        ids = {'expected_repo_id': expected_repo_id, 'expected_company_id': expected_company_id}
        parent_standard = Path(cur.db_path) if cur else None
        parent_derived = _derived_sibling(parent_standard) if parent_standard is not None else None
        published, generations = [], []
        for i, commit in enumerate(win.commits):
            built = adv1.build_standard_generation_for_commit(repository, commit, directory, **ids)
            if win.is_bootstrap and i == 0:
                wide.build_projection_wide(built.db_path, directory=directory, **ids)
                derived = fts.build_projection_fts(built.db_path, directory=directory, **ids)
            else:
                derived = der.build_incremental_derived(built.db_path, parent_derived,
                                                         directory=directory, **ids)
            serving = _publish_bundle(root, directory, built, derived, **ids, now=now)
            parent_standard, parent_derived = Path(serving.db_path), Path(derived.db_path)
            published.append(commit)
            generations.append((commit, str(parent_standard), str(parent_derived)))
        return AdvanceReport(current_commit, published[-1] if published else current_commit,
                             tuple(published), tuple(generations), False)
    except Exception as exc:
        raise AdvanceServiceError('cannot advance projection: ' + str(exc)) from exc
