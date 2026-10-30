"""Appendix D3 v1.2: publication, recovery, byte guards and failure safety."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading

import pytest

from app.facts_tree import git_format, projection_router as router
from app.facts_tree.projection_generation import BuiltProjectionGeneration, build_projection_generation
from app.facts_tree.projection_plan import build_projection_plan
from app.facts_tree.record_tree import build_record_tree
from tests.facts_tree_full_fixture import SCOPE, full_tree


STAMP = "2026-10-30T12:00:00+00:00"
LATER = "2026-10-30T13:00:00+00:00"
POINTER_KEYS = {"format", "repo_id", "company_id", "generation_id", "db_path",
                "projected_commit", "render_sha256", "published_at"}
SERVING_FIELDS = [
    "generation_id", "db_path", "repo_id", "company_id", "projected_commit",
    "head_oid", "object_format", "render_sha256", "record_count", "active_fact_count",
    "projection_schema_version", "builder_version", "renderer_version", "required_indexes",
]


def _build(directory, commit="c", *, repo_id="repo-test", company_id="company-test", empty=False):
    payloads = {} if empty else full_tree()
    if not empty:
        payloads[git_format.record_path("repository_meta")] = {
            "schema_version": 1, "format_version": 1, "installation_id": "install-test"}
    for payload in payloads.values():
        payload.update(repo_id=repo_id, company_id=company_id)
    records = [(path, git_format.dumps_record(payload)) for path, payload in payloads.items()]
    tree = build_record_tree(records, repo_id=repo_id, company_id=company_id,
                             commit_oid=commit * 40, head_oid="d" * 40)
    active = frozenset(value["fact_uid"] for value in tree.ordered.values()
                       if value.get("record_type") == "fact_current" and value["status"] == "active")
    return build_projection_generation(build_projection_plan(tree), records, active, directory,
                                       expected_repo_id=repo_id, expected_company_id=company_id)


def _publish(built, root, directory, **kwargs):
    return router.publish_generation(built, root, directory, **{
        "expected_repo_id": SCOPE["repo_id"], "expected_company_id": SCOPE["company_id"],
        "now": lambda: STAMP, **kwargs})


def _current(root, repo_id="repo-test", company_id="company-test"):
    return root / git_format.encode_id(repo_id) / git_format.encode_id(company_id) / "current.json"


def _load(root):
    return router.load_current(root, **SCOPE)


def _snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _mutate_meta(built, key, value):
    with closing(sqlite3.connect(built.db_path)) as connection:
        with connection:
            if value is None:
                connection.execute("DELETE FROM generation_meta WHERE key=?", (key,))
            else:
                connection.execute("INSERT OR REPLACE INTO generation_meta VALUES (?, ?)", (key, value))


def _tamper(built, *, update_row_digest=False):
    with closing(sqlite3.connect(built.db_path)) as connection:
        path, content = connection.execute(
            "SELECT path, canonical_bytes FROM record_canonical ORDER BY path LIMIT 1").fetchone()
        changed = bytes([content[0] ^ 1]) + content[1:]
        assert sum(a != b for a, b in zip(changed, content)) == 1
        with connection:
            connection.execute("UPDATE record_canonical SET canonical_bytes=? WHERE path=?", (changed, path))
            if update_row_digest:
                connection.execute("UPDATE record_canonical SET sha256=? WHERE path=?",
                                   (sha256(changed).hexdigest(), path))
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_publish_load_open_independent_pointer_and_ro(tmp_path):
    directory, root = tmp_path / "generations", tmp_path / "registry"
    built = _build(directory)
    before = _snapshot(directory)
    serving = _publish(built, root, directory)
    path = _current(root)
    raw = path.read_bytes()
    pointer = json.loads(raw)
    assert set(pointer) == POINTER_KEYS
    assert pointer == {"format": "projection-current@1", **SCOPE,
                       "generation_id": built.generation_id, "db_path": str(Path(built.db_path).resolve()),
                       "projected_commit": built.projected_commit, "render_sha256": built.render_sha256,
                       "published_at": STAMP}
    assert raw == json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert [field.name for field in fields(serving)] == SERVING_FIELDS
    assert serving.record_count == 19 and serving.active_fact_count == 1
    assert serving.required_indexes == ("canonical_records",)
    assert all(type(getattr(serving, key)) is int for key in SERVING_FIELDS[8:13])
    with pytest.raises(FrozenInstanceError):
        serving.record_count = 0
    assert _load(root) == serving
    opened, connection = router.open_current(root, **SCOPE)
    with closing(connection):
        assert opened == serving
        assert connection.execute("SELECT count(*) FROM record_canonical").fetchone() == (19,)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM generation_meta")
    assert _snapshot(directory) == before


def test_recover_fresh_process_and_no_automatic_selection(tmp_path):
    directory, root = tmp_path / "generations", tmp_path / "registry"
    built = _build(directory)
    _build(directory, "e")
    assert router.recover_published(root, directory, **SCOPE) is None
    assert not root.exists()
    serving = _publish(built, root, directory)
    # Real interpreter with fresh module state, inherited test isolation.
    script = (
        "from app.facts_tree.projection_router import recover_published\n"
        "import sys\n"
        "s = recover_published(sys.argv[1], sys.argv[2], repo_id='repo-test', company_id='company-test')\n"
        "print(s.generation_id)\n")
    result = subprocess.run([os.sys.executable, "-c", script, str(root), str(directory)],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == serving.generation_id
    _current(root).unlink()
    (directory / ".gen-x.tmp-pid").write_bytes(b"unfinished")
    assert router.recover_published(root, directory, **SCOPE) is None
    assert Path(built.db_path).exists()


def test_switch_keeps_old_generation_and_existing_reader(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    a_bytes = Path(a.db_path).read_bytes()
    first = _publish(a, root, directory)
    _, connection = router.open_current(root, **SCOPE)
    with closing(connection):
        second = _publish(b, root, directory, now=lambda: LATER)
        assert first.generation_id != second.generation_id
        assert _load(root) == second
        assert connection.execute("SELECT value FROM generation_meta WHERE key='generation_id'").fetchone() == (a.generation_id,)
    assert json.loads(_current(root).read_text())["published_at"] == LATER
    assert Path(a.db_path).read_bytes() == a_bytes
    with closing(sqlite3.connect(Path(a.db_path).as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_idempotent_revalidates_without_clock_replace_or_write(tmp_path, monkeypatch):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    first = _publish(built, root, directory)
    path = _current(root)
    before = (path.stat().st_ino, path.stat().st_mtime_ns, _snapshot(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("duplicate publish must not call clock or replace")

    monkeypatch.setattr(router.os, "replace", forbidden)
    assert _publish(built, root, directory, now=forbidden) == first
    assert (path.stat().st_ino, path.stat().st_mtime_ns, _snapshot(tmp_path)) == before
    _tamper(built)
    with pytest.raises(router.ProjectionRouterError):
        _publish(built, root, directory)
    assert path.read_bytes() == before[2][str(path.relative_to(tmp_path))]


@pytest.mark.parametrize("damage", ["not_ready", "repo", "company", "outside", "directory", "missing",
                                  "id", "commit", "render", "count", "canonical"])
def test_publish_failure_preserves_current(tmp_path, damage):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    serving = _publish(a, root, directory)
    before = _snapshot(root)
    if damage == "not_ready":
        b = replace(b, ready=False)
    elif damage in {"repo", "company"}:
        _mutate_meta(b, damage + "_id", "other")
    elif damage == "outside":
        outside = tmp_path / "outside" / Path(b.db_path).name
        outside.parent.mkdir()
        shutil.copyfile(b.db_path, outside)
        b = replace(b, db_path=str(outside))
    elif damage == "directory":
        b = replace(b, db_path=str(directory))
    elif damage == "missing":
        b = replace(b, db_path=str(directory / "missing.sqlite"))
    elif damage == "id":
        b = replace(b, generation_id="f" * 32)
    elif damage == "commit":
        b = replace(b, projected_commit="f" * 40)
    elif damage == "render":
        b = replace(b, render_sha256="f" * 64)
    elif damage == "count":
        b = replace(b, record_count=b.record_count + 1)
    else:
        _tamper(b)
    with pytest.raises((ValueError, router.ProjectionRouterError)) as error:
        _publish(b, root, directory)
    if damage == "outside":
        assert type(error.value) is ValueError
    assert _snapshot(root) == before
    assert _load(root) == serving


@pytest.mark.parametrize("update_row_digest", [False, True])
@pytest.mark.parametrize("entry", ["load", "open", "recover"])
def test_loading_detects_byte_tamper_even_with_healthy_sqlite(tmp_path, update_row_digest, entry):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    _tamper(built, update_row_digest=update_row_digest)
    call = {"load": lambda: _load(root), "open": lambda: router.open_current(root, **SCOPE),
            "recover": lambda: router.recover_published(root, directory, **SCOPE)}[entry]
    with pytest.raises(router.CurrentProjectionUnavailable, match="checksum|render"):
        call()


@pytest.mark.parametrize("damage", ["extra", "missing", "json", "format", "type", "bool", "list",
                                  "nan", "duplicate", "relative", "traversal", "repo", "company",
                                  "generation", "render", "commit", "stamp"])
def test_corrupt_pointer_fails_closed(tmp_path, damage):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    path = _current(root)
    pointer = json.loads(path.read_text())
    if damage == "extra":
        pointer["extra"] = "x"
    elif damage == "missing":
        del pointer["generation_id"]
    elif damage == "format":
        pointer["format"] = "projection-current@2"
    elif damage == "type":
        pointer["generation_id"] = 1
    elif damage == "bool":
        pointer["generation_id"] = True
    elif damage == "list":
        pointer = []
    elif damage == "relative":
        pointer["db_path"] = "gen-relative.sqlite"
    elif damage == "traversal":
        pointer["db_path"] = str(directory / ".." / directory.name / Path(built.db_path).name)
    elif damage in {"repo", "company"}:
        pointer[damage + "_id"] = "other"
    elif damage == "generation":
        pointer["generation_id"] = "f" * 32
    elif damage == "render":
        pointer["render_sha256"] = "f" * 64
    elif damage == "commit":
        pointer["projected_commit"] = "f" * 40
    elif damage == "stamp":
        pointer["published_at"] = "no timestamp"
    raw = json.dumps(pointer)
    if damage == "json":
        raw = "{"
    elif damage == "nan":
        raw = raw.replace('"' + STAMP + '"', "NaN")
    elif damage == "duplicate":
        raw = raw[:-1] + ',"format":"projection-current@1"}'
    path.write_text(raw)
    with pytest.raises(router.CurrentProjectionUnavailable):
        _load(root)
    with pytest.raises(router.CurrentProjectionUnavailable):
        router.recover_published(root, directory, **SCOPE)


def test_missing_current_and_missing_generation_never_fall_back(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    with pytest.raises(router.NoCurrentProjection):
        _load(root)
    _publish(a, root, directory)
    Path(a.db_path).unlink()
    with pytest.raises(router.CurrentProjectionUnavailable) as error:
        _load(root)
    assert error.value.__cause__ is not None
    assert a.generation_id in str(error.value) and "company-test" in str(error.value)
    assert Path(b.db_path).exists()


@pytest.mark.parametrize("key,value", [
    ("status", "staging"), ("status", None), ("head_oid", None), ("unexpected", "x"),
    ("record_count", "true"), ("record_count", "-1"), ("record_count", "1.0"),
    ("record_count", "１"), ("record_count", str(2**63)), ("record_count", "20"),
    ("active_fact_count", "-1"), ("projection_schema_version", "2"),
    ("builder_version", "false"), ("renderer_version", "0"),
    ("required_indexes", "{}"), ("required_indexes", '["other"]'),
    ("required_indexes", '["canonical_records","other"]'), ("required_indexes", "NaN"),
    ("object_format", "unknown"), ("generation_id", "f" * 32),
])
def test_meta_damage_rejected_for_publish_and_load(tmp_path, key, value):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    pointer_before = _snapshot(root)
    _mutate_meta(built, key, value)
    with pytest.raises(router.CurrentProjectionUnavailable):
        _load(root)
    with pytest.raises(router.ProjectionRouterError):
        _publish(built, root, directory)
    assert _snapshot(root) == pointer_before


@pytest.mark.parametrize("damage", ["length", "digest", "count", "blob_type", "sqlite"])
def test_canonical_shape_integrity_and_count_guards(tmp_path, damage):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    if damage == "sqlite":
        Path(built.db_path).write_bytes(b"not a database")
    else:
        sql = {"length": "UPDATE record_canonical SET byte_length=byte_length+1",
               "digest": "UPDATE record_canonical SET sha256='bad'",
               "count": "DELETE FROM record_canonical WHERE path=(SELECT path FROM record_canonical LIMIT 1)",
               "blob_type": "UPDATE record_canonical SET canonical_bytes='not bytes'"}[damage]
        with closing(sqlite3.connect(built.db_path)) as conn:
            with conn:
                conn.execute(sql)
    with pytest.raises(router.CurrentProjectionUnavailable):
        _load(root)


@pytest.mark.parametrize("kind", ["file", "parent", "escape", "dangling"])
def test_generation_symlinks_rejected(tmp_path, kind):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    source = Path(built.db_path)
    if kind == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        linked = alias / source.name
    else:
        target = tmp_path / "outside" / source.name
        target.parent.mkdir()
        if kind != "dangling":
            shutil.copyfile(source, target)
        source.unlink()
        source.symlink_to(target)
        linked = source
    bad = replace(built, db_path=str(linked))
    with pytest.raises(ValueError):
        _publish(bad, root, directory)
    if kind == "parent":
        pointer = json.loads(_current(root).read_text())
        pointer["db_path"] = str(linked)
        _current(root).write_text(json.dumps(pointer))
    with pytest.raises(router.CurrentProjectionUnavailable):
        _load(root)


def test_pointer_and_scope_symlinks_rejected(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    path = _current(root)
    outside = tmp_path / "pointer-copy"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(router.CurrentProjectionUnavailable):
        _load(root)
    with pytest.raises(router.ProjectionRouterError):
        _publish(built, root, directory)
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(router.CurrentProjectionUnavailable):
        _load(root)


def test_recover_rejects_wrong_generations_boundary(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    with pytest.raises(router.CurrentProjectionUnavailable, match="outside"):
        router.recover_published(root, tmp_path / "gens-other", **SCOPE)
    assert router.recover_published(root, tmp_path, **SCOPE) == _load(root)


def test_retire_discovery_is_meta_only_scoped_and_does_not_modify(tmp_path, monkeypatch):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    other = _build(directory, "f", company_id="other-company", empty=True)
    _publish(a, root, directory)
    _tamper(b)  # Discovery deliberately does not scan or validate record bytes.
    (directory / ".gen-x.tmp-pid").write_bytes(b"unfinished")
    (directory / ("gen-" + "0" * 32 + ".sqlite")).write_bytes(b"bad sqlite")
    partial = _build(directory, "a")
    _mutate_meta(partial, "status", None)
    alias = directory / ("gen-" + "1" * 32 + ".sqlite")
    alias.symlink_to(b.db_path)
    (directory / "nested").mkdir()
    shutil.copyfile(b.db_path, directory / "nested" / Path(b.db_path).name)
    before = _snapshot(tmp_path)
    connect = sqlite3.connect

    def meta_only(database, *args, **kwargs):
        assert str(database).endswith("?mode=ro") and kwargs.get("uri") is True
        connection = connect(database, *args, **kwargs)
        connection.set_authorizer(lambda action, arg1, arg2, db, source:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and arg1 == "record_canonical"
            else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(router.sqlite3, "connect", meta_only)
    assert router.find_retire_candidates(root, directory, **SCOPE) == (b.generation_id,)
    assert _snapshot(tmp_path) == before
    _current(root).unlink()
    assert router.find_retire_candidates(root, directory, **SCOPE) == tuple(sorted((a.generation_id, b.generation_id)))
    assert Path(other.db_path).exists() and Path(partial.db_path).exists()


def test_empty_generation_and_absent_discovery_create_nothing(tmp_path):
    assert router.find_retire_candidates(tmp_path / "none", tmp_path / "missing", **SCOPE) == ()
    assert list(tmp_path.iterdir()) == []
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory, empty=True)
    serving = _publish(built, root, directory)
    assert serving.record_count == 0 and serving.render_sha256 == sha256(b"").hexdigest()
    assert _load(root) == serving


@pytest.mark.parametrize("scope", [("a__", "b"), ("a", "__b"), ("../库 /", "..\\公司 / x")])
def test_path_encoding_and_colliding_flat_names_are_isolated(tmp_path, scope):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    other_scope = ("a", "__b") if scope == ("a__", "b") else ("a__", "b")
    for repo, company in (scope, other_scope):
        built = _build(directory, repo_id=repo, company_id=company, empty=True)
        _publish(built, root, directory, expected_repo_id=repo, expected_company_id=company)
    paths = [_current(root, *identity) for identity in (scope, other_scope)]
    assert paths[0] != paths[1]
    for path, (repo, company) in zip(paths, (scope, other_scope)):
        assert path.resolve().is_relative_to(root.resolve())
        assert path.relative_to(root).parts == (git_format.encode_id(repo), git_format.encode_id(company), "current.json")
        assert router.load_current(root, repo_id=repo, company_id=company).repo_id == repo
        assert json.loads(path.read_text())["company_id"] == company


def test_concurrent_publish_readers_observe_only_complete_pointers(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    _publish(a, root, directory)
    ready = threading.Barrier(3)
    done = threading.Event()
    identities = {a.generation_id, b.generation_id}

    def publisher(built):
        ready.wait(timeout=10)
        for _ in range(8):
            _publish(built, root, directory)

    def observer():
        ready.wait(timeout=10)
        observations = 0
        while not done.is_set() or observations < 8:
            pointer = json.loads(_current(root).read_bytes())
            assert set(pointer) == POINTER_KEYS and pointer["generation_id"] in identities
            assert _load(root).generation_id in identities
            observations += 1
        return observations

    with ThreadPoolExecutor(max_workers=3) as pool:
        read = pool.submit(observer)
        writes = [pool.submit(publisher, built) for built in (a, b)]
        try:
            for write in writes:
                write.result(timeout=30)
        finally:
            done.set()
        assert read.result(timeout=30) >= 8
    assert _load(root).generation_id in identities
    assert [p.name for p in _current(root).parent.iterdir()] == ["current.json"]
    assert Path(a.db_path).exists() and Path(b.db_path).exists()


@pytest.mark.parametrize("stage", ["file_fsync", "replace"])
def test_atomic_write_failure_preserves_pointer_and_cleans_only_own_temp(tmp_path, monkeypatch, stage):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    _publish(a, root, directory)
    before = _snapshot(tmp_path)
    real_fsync = os.fsync

    def fsync(fd):
        import stat
        if stage == "file_fsync" and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected file fsync failure")
        return real_fsync(fd)

    def failed_replace(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(router.os, "fsync", fsync)
    if stage == "replace":
        monkeypatch.setattr(router.os, "replace", failed_replace)
    with pytest.raises(router.ProjectionRouterError, match="injected") as error:
        _publish(b, root, directory)
    assert isinstance(error.value.__cause__, OSError)
    assert _snapshot(tmp_path) == before
    assert _load(root).generation_id == a.generation_id


@pytest.mark.parametrize("field,value,error", [
    ("record_count", True, TypeError), ("record_count", -1, ValueError),
    ("ready", 1, TypeError), ("ready", False, ValueError),
    ("db_path", b"bytes", TypeError), ("db_path", "", ValueError),
    ("generation_id", 1, TypeError), ("projected_commit", None, TypeError),
    ("render_sha256", False, TypeError),
])
def test_built_argument_types_before_any_pointer_io(tmp_path, field, value, error):
    built = replace(BuiltProjectionGeneration("unused", "a" * 32, "c" * 40, "d" * 64, 0, True), **{field: value})
    with pytest.raises(error):
        _publish(built, tmp_path / "registry", tmp_path / "gens")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("argument,value,error", [
    ("root", None, TypeError), ("root", b"bytes", TypeError), ("root", "", ValueError),
    ("generations_dir", None, TypeError), ("generations_dir", b"bytes", TypeError),
    ("generations_dir", "", ValueError), ("expected_repo_id", True, TypeError),
    ("expected_company_id", 1, TypeError), ("expected_repo_id", "", ValueError),
    ("expected_company_id", "", ValueError), ("now", 1, TypeError),
])
def test_publish_parameter_types(tmp_path, argument, value, error):
    options = dict(built=BuiltProjectionGeneration("unused", "a" * 32, "c" * 40, "d" * 64, 0, True),
                   root=tmp_path / "registry", generations_dir=tmp_path / "gens",
                   expected_repo_id="repo-test", expected_company_id="company-test")
    options[argument] = value
    with pytest.raises(error):
        router.publish_generation(**options)
    assert list(tmp_path.iterdir()) == []


def test_exact_built_type_default_utc_clock_and_root_file(tmp_path):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    with pytest.raises(TypeError):
        _publish(object(), root, directory)
    before = datetime.now(timezone.utc)
    _publish(built, root, directory, now=None)
    after = datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(json.loads(_current(root).read_text())["published_at"])
    assert before <= stamp <= after and stamp.utcoffset().total_seconds() == 0
    bad_root = tmp_path / "file"
    bad_root.write_text("preserve")
    with pytest.raises(router.ProjectionRouterError):
        _publish(built, bad_root, directory)
    assert bad_root.read_text() == "preserve"


@pytest.mark.parametrize("stamp,error", [(True, TypeError), (None, TypeError),
                                        ("invalid", ValueError),
                                        ("2026-10-30T12:00:00", ValueError),
                                        ("2026-10-30T12:00:00+08:00", ValueError)])
def test_invalid_clock_result_preserves_current(tmp_path, stamp, error):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    a, b = _build(directory), _build(directory, "e")
    _publish(a, root, directory)
    before = _snapshot(root)
    with pytest.raises(error):
        _publish(b, root, directory, now=lambda: stamp)
    assert _snapshot(root) == before


def test_open_revalidates_new_connection_and_closes_it_on_failure(tmp_path, monkeypatch):
    directory, root = tmp_path / "gens", tmp_path / "registry"
    built = _build(directory)
    _publish(built, root, directory)
    original_connect = sqlite3.connect
    connections = []

    def connect(database, *args, **kwargs):
        if connections:
            _tamper(built)
        connection = original_connect(database, *args, **kwargs)
        connections.append(connection)
        return connection

    # Patch only the router's connection helper so fixture tampering stays independent.
    def ro_connect(db):
        return connect(db.as_uri() + "?mode=ro", uri=True)

    monkeypatch.setattr(router, "_connect", ro_connect)
    with pytest.raises(router.CurrentProjectionUnavailable):
        router.open_current(root, **SCOPE)
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
