"""D14-2 real jointly published bundles, failure gates, and ownership checks."""
import ast
from contextlib import closing
from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
from pathlib import Path, PurePath
import shutil
import sqlite3

import pytest

from app.facts_tree import projection_bundle as bundle, projection_router as router
from app.facts_tree.record_schema import SCHEMA_COLUMNS
from tests.facts_tree_full_fixture import SCOPE
from tests.test_projection_advance_service import _full, _run, _pointer
from tests.test_projection_incremental import chain  # noqa: F401


IDENTITY = ("generation_id", "projected_commit", "render_sha256", "repo_id", "company_id", "status")


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_calls = 0
        self.statements = []
        self.set_trace_callback(self.statements.append)

    def close(self):
        self.close_calls += 1
        super().close()


def track_connections(monkeypatch):
    connect = sqlite3.connect
    opened = []

    def tracked(database, *args, **kwargs):
        assert kwargs.get("uri") is True and str(database).endswith("?mode=ro")
        kwargs["factory"] = TrackedConnection
        connection = connect(database, *args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(router.sqlite3, "connect", tracked)
    return opened


def assert_closed(connections):
    for connection in connections:
        assert connection.close_calls == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def inventory(directory):
    return {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, sha256(p.read_bytes()).hexdigest())
            for p in directory.iterdir() if p.is_file()}


def assert_no_journals(directory):
    assert not any(p.name.endswith(("-wal", "-shm", "-journal")) for p in directory.rglob("*"))


@pytest.fixture
def published(chain, tmp_path):
    repo, commits = chain
    root, directory = tmp_path / "root", tmp_path / "generations ?#中文"
    _run(repo, root, directory, target=commits[1])
    serving = router.load_current(root, **SCOPE)
    return root, directory, serving, Path(bundle.derived_path(serving))


def change_meta(path, table, key, value):
    with closing(sqlite3.connect(path)) as connection, connection:
        if value is None:
            connection.execute("DELETE FROM " + table + " WHERE key=?", (key,))
        else:
            connection.execute("UPDATE " + table + " SET value=? WHERE key=?", (value, key))


def test_four_real_generations_pair_readonly_and_context_ownership(chain, tmp_path, monkeypatch):
    from app.facts_tree.projection_advance_service import _derived_sibling

    repo, commits = chain
    root, directory = tmp_path / "root", tmp_path / "generations"
    for commit in commits:
        _run(repo, root, directory, target=commit)
        before = inventory(directory)
        with monkeypatch.context() as patch:
            opened = track_connections(patch)
            with bundle.open_bundle(root, **SCOPE) as handle:
                assert type(handle) is bundle.ProjectionBundle
                assert handle.serving.projected_commit == commit
                pointer = json.loads(_pointer(root).read_text())
                for key, value in router._identity(handle.serving).items():
                    assert pointer[key] == value
                assert handle.standard.execute("SELECT * FROM record_canonical WHERE 0").fetchall() == []
                assert handle.derived.execute("SELECT * FROM dw_evidence WHERE 0").fetchall() == []
                expected = {key: "ready" if key == "status" else getattr(handle.serving, key)
                            for key in IDENTITY}
                for table in ("wide_meta", "fts_meta"):
                    actual = dict(handle.derived.execute("SELECT key,value FROM " + table))
                    assert {key: actual[key] for key in IDENTITY} == expected
                standard_path = Path(handle.standard.execute("PRAGMA database_list").fetchone()[2])
                derived = Path(handle.derived.execute("PRAGMA database_list").fetchone()[2])
                assert derived == bundle.derived_path(handle.serving) == _derived_sibling(standard_path)
                assert handle.derived.execute("PRAGMA query_only").fetchone() == (1,)
                for connection in (handle.standard, handle.derived):
                    assert connection.total_changes == 0
                    # mode=ro still protects the connection after query_only is disabled.
                    connection.execute("PRAGMA query_only=OFF")
                    with pytest.raises(sqlite3.OperationalError, match="readonly"):
                        connection.execute("CREATE TABLE forbidden(x)")
                with pytest.raises(FrozenInstanceError):
                    handle.serving = None
            handle.close()
            assert len(opened) == 3
            assert_closed(opened)
        assert inventory(directory) == before
        assert_no_journals(directory)


def test_repeat_open_is_fresh_and_explicit_close_is_idempotent(published, monkeypatch):
    root, directory, _, _ = published
    before = inventory(directory)
    opened = track_connections(monkeypatch)
    first, second = bundle.open_bundle(root, **SCOPE), bundle.open_bundle(root, **SCOPE)
    assert first.serving == second.serving
    assert first.standard is not second.standard and first.derived is not second.derived
    first.close()
    first.close()
    assert second.derived.execute("SELECT count(*) FROM dw_evidence").fetchone()[0] > 0
    second.close()
    assert_closed(opened)
    assert inventory(directory) == before


def test_context_does_not_swallow_body_exception(published, monkeypatch):
    opened = track_connections(monkeypatch)
    error = RuntimeError("body error")
    with pytest.raises(RuntimeError) as caught:
        with bundle.open_bundle(published[0], **SCOPE):
            raise error
    assert caught.value is error
    assert_closed(opened)


@pytest.mark.parametrize("table", ["wide_meta", "fts_meta"])
@pytest.mark.parametrize("key", IDENTITY)
@pytest.mark.parametrize("damage", ["changed", "missing", "empty"])
def test_meta_identity_fail_closed(published, monkeypatch, table, key, damage):
    root, directory, serving, derived = published
    value = {"changed": "private-value-must-not-leak", "missing": None, "empty": ""}[damage]
    change_meta(derived, table, key, value)
    before = inventory(directory)
    opened = track_connections(monkeypatch)
    with pytest.raises(bundle.BundleUnavailable) as caught:
        bundle.open_bundle(root, **SCOPE)
    message = str(caught.value)
    assert table in message and key in message
    assert serving.generation_id in message and SCOPE["company_id"] in message
    assert "private-value-must-not-leak" not in message
    assert len(opened) == 3
    assert_closed(opened)
    assert inventory(directory) == before
    assert_no_journals(directory)


@pytest.mark.parametrize("table", ["wide_meta", "fts_meta"])
@pytest.mark.parametrize("damage", ["no_primary_key", "null_value", "blob_value", "null_key"])
def test_meta_storage_must_be_valid_text(published, monkeypatch, table, damage):
    root, directory, _, derived = published
    with closing(sqlite3.connect(derived)) as connection, connection:
        if damage in {"no_primary_key", "null_value"}:
            rows = connection.execute("SELECT key,value FROM " + table).fetchall()
            connection.execute("DROP TABLE " + table)
            key_decl = "TEXT" if damage == "no_primary_key" else "TEXT PRIMARY KEY"
            connection.execute("CREATE TABLE " + table + " (key " + key_decl + ", value TEXT)")
            connection.executemany("INSERT INTO " + table + " VALUES (?,?)", rows)
            if damage == "null_value":
                connection.execute("UPDATE " + table + " SET value=NULL WHERE key='generation_id'")
        elif damage == "blob_value":
            connection.execute("UPDATE " + table + " SET value=? WHERE key='generation_id'", (b"bad",))
        else:
            connection.execute("INSERT INTO " + table + " VALUES (NULL,'value')")
    opened = track_connections(monkeypatch)
    with pytest.raises(bundle.BundleUnavailable):
        bundle.open_bundle(root, **SCOPE)
    assert_closed(opened)
    assert_no_journals(directory)


@pytest.mark.parametrize("damage", ["missing", "renamed", "symlink", "dangling_symlink", "directory",
                                    "outside", "wrong_name", "other_generation", "corrupt"])
def test_path_and_file_failures_are_closed(published, chain, tmp_path, monkeypatch, damage):
    root, directory, _, derived = published
    if damage == "missing":
        derived.unlink()
    elif damage == "renamed":
        derived.rename(derived.with_suffix(".renamed"))
    elif damage in {"symlink", "dangling_symlink"}:
        target = tmp_path / "external.sqlite"
        if damage == "symlink":
            shutil.copyfile(derived, target)
        derived.unlink()
        derived.symlink_to(target)
    elif damage == "directory":
        derived.unlink()
        derived.mkdir()
    elif damage in {"outside", "wrong_name"}:
        target = (tmp_path if damage == "outside" else directory) / "derived-other.sqlite"
        shutil.copyfile(derived, target)
        monkeypatch.setattr(bundle, "derived_path", lambda serving: PurePath(target))
    elif damage == "other_generation":
        repo, commits = chain
        _, other = _full(repo, commits[2], tmp_path / "other-generation")
        shutil.copyfile(other.db_path, derived)
    else:
        derived.write_bytes(b"not a SQLite database")
    before = inventory(directory)
    opened = track_connections(monkeypatch)
    with pytest.raises(bundle.BundleUnavailable):
        bundle.open_bundle(root, **SCOPE)
    assert len(opened) == (3 if damage in {"other_generation", "corrupt"} else 2)
    assert_closed(opened)
    assert inventory(directory) == before
    assert_no_journals(directory)


@pytest.mark.parametrize("damage", ["wide_meta", "fts_meta", "missing_wide", "extra_wide", "extra_table",
                                    "fake_shadow", "ordinary_fts", "wide_view", "wide_virtual"])
def test_table_and_virtual_table_gates(published, monkeypatch, damage):
    root, directory, _, derived = published
    with closing(sqlite3.connect(derived)) as connection, connection:
        if damage in {"wide_meta", "fts_meta"}:
            connection.execute("DROP TABLE " + damage)
        elif damage == "missing_wide":
            connection.execute("DROP TABLE dw_evidence")
        elif damage in {"extra_wide", "extra_table", "fake_shadow"}:
            name = {"extra_wide": "dw_extra", "extra_table": "extra", "fake_shadow": "fts_records_spoof"}[damage]
            connection.execute("CREATE TABLE " + name + " (x)")
        elif damage == "ordinary_fts":
            connection.execute("DROP TABLE fts_records")
            connection.execute("CREATE TABLE fts_records(path,family,record_text)")
        elif damage == "wide_view":
            connection.execute("DROP TABLE dw_evidence")
            connection.execute("CREATE VIEW dw_evidence AS SELECT 1 AS x")
        else:
            connection.execute("DROP TABLE dw_evidence")
            connection.execute("CREATE VIRTUAL TABLE dw_evidence USING fts5(x)")
    before = inventory(directory)
    opened = track_connections(monkeypatch)
    with pytest.raises(bundle.BundleUnavailable):
        bundle.open_bundle(root, **SCOPE)
    assert len(opened) == 3
    assert_closed(opened)
    assert inventory(directory) == before
    assert_no_journals(directory)


def test_integrity_non_ok_and_verification_exception_release_both(published, monkeypatch):
    root, _, _, _ = published
    opened = track_connections(monkeypatch)
    verify = bundle._verify_derived

    class BadIntegrity:
        def execute(self, sql):
            assert sql == "PRAGMA integrity_check"
            return self

        def fetchone(self):
            return ("broken page",)

    def fail_integrity(connection, path, serving, repo_id, company_id):
        verify(BadIntegrity(), path, serving, repo_id, company_id)

    monkeypatch.setattr(bundle, "_verify_derived", fail_integrity)
    with pytest.raises(bundle.BundleUnavailable, match="integrity_check"):
        bundle.open_bundle(root, **SCOPE)
    assert_closed(opened)


@pytest.mark.parametrize("stage", ["boundary", "connect", "verify"])
def test_late_failure_releases_owned_connections_without_leaking_detail(published, monkeypatch, stage):
    root, _, _, _ = published
    opened = track_connections(monkeypatch)
    if stage == "boundary":
        original_open = router.open_current

        def remove_standard(*args, **kwargs):
            serving, connection = original_open(*args, **kwargs)
            Path(serving.db_path).unlink()
            return serving, connection

        monkeypatch.setattr(router, "open_current", remove_standard)
    elif stage == "connect":
        original_connect = router._connect

        def fail_connect(path):
            if path.name.startswith("derived-"):
                raise sqlite3.OperationalError("private-value-must-not-leak")
            return original_connect(path)

        monkeypatch.setattr(router, "_connect", fail_connect)
    else:
        def fail_verify(*args):
            raise RuntimeError("private-value-must-not-leak")

        monkeypatch.setattr(bundle, "_verify_derived", fail_verify)
    with pytest.raises(bundle.BundleUnavailable) as caught:
        bundle.open_bundle(root, **SCOPE)
    assert "private-value-must-not-leak" not in str(caught.value)
    assert len(opened) == (3 if stage == "verify" else 2)
    assert_closed(opened)


def test_no_current_passes_through(tmp_path, monkeypatch):
    opened = track_connections(monkeypatch)
    with pytest.raises(router.NoCurrentProjection):
        bundle.open_bundle(tmp_path / "unpublished", **SCOPE)
    assert opened == []


@pytest.mark.parametrize("damage", ["meta", "bytes", "missing", "pointer"])
def test_bad_standard_keeps_router_exception(published, monkeypatch, damage):
    root, _, serving, _ = published
    if damage == "meta":
        change_meta(serving.db_path, "generation_meta", "status", "staging")
    elif damage == "bytes":
        with closing(sqlite3.connect(serving.db_path)) as connection, connection:
            connection.execute("UPDATE record_canonical SET canonical_bytes=?", (b"corrupt",))
    elif damage == "missing":
        Path(serving.db_path).unlink()
    else:
        _pointer(root).write_text("{")
    opened = track_connections(monkeypatch)
    with pytest.raises(router.CurrentProjectionUnavailable) as caught:
        bundle.open_bundle(root, **SCOPE)
    assert type(caught.value) is router.CurrentProjectionUnavailable
    assert len(opened) <= 1
    assert_closed(opened)


@pytest.mark.parametrize("exception", [router.NoCurrentProjection, router.CurrentProjectionUnavailable,
                                      TypeError, ValueError])
def test_standard_delegate_exceptions_preserve_identity(monkeypatch, exception):
    error = exception("original")

    def fail_open(*args, **kwargs):
        raise error

    monkeypatch.setattr(router, "open_current", fail_open)
    with pytest.raises(exception) as caught:
        bundle.open_bundle("unused", **SCOPE)
    assert caught.value is error


@pytest.mark.parametrize("name", ["ordinary.sqlite", "derived-x.sqlite", "gen-x.txt", "gen-x.sqlite.bak", ""])
def test_derived_path_rejects_bad_standard_names(name):
    serving = router.ServingGeneration("a" * 32, name, "repo", "company", "b" * 40, "c" * 40,
                                        "sha1", "d" * 64, 0, 0, 1, 1, 1, ("canonical_records",))
    with pytest.raises(bundle.BundleUnavailable):
        bundle.derived_path(serving)


def test_derived_path_is_pure_and_keeps_parent():
    serving = router.ServingGeneration("a" * 32, "/does-not-exist/?#/gen-name.sqlite", "repo", "company",
                                        "b" * 40, "c" * 40, "sha1", "d" * 64, 0, 0, 1, 1, 1,
                                        ("canonical_records",))
    assert bundle.derived_path(serving) == PurePath("/does-not-exist/?#/derived-name.sqlite")
    assert type(bundle.derived_path(serving)) is type(PurePath())


def test_import_and_sql_guards():
    source = Path(bundle.__file__).read_text()
    tree = ast.parse(source)
    imports = []
    sql_literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            if node.module == "app.facts_tree":
                assert [(alias.name, alias.asname) for alias in node.names] == [("projection_router", "router")]
            elif node.module == "app.facts_tree.record_schema":
                assert [alias.name for alias in node.names] == ["SCHEMA_COLUMNS"]
            else:
                imports.append(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip().upper()
            assert not any(value.startswith(prefix) for prefix in (
                "INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "ATTACH ", "DETACH "))
            assert "WRITABLE_SCHEMA" not in value
            if value.startswith("PRAGMA "):
                assert value in {"PRAGMA INTEGRITY_CHECK", "PRAGMA QUERY_ONLY=ON", "PRAGMA TABLE_INFO("}
            if value.startswith("SELECT "):
                sql_literals.append(value)
    assert set(imports) <= {"contextlib", "dataclasses", "pathlib", "sqlite3"}
    assert set(sql_literals) == {
        "SELECT NAME, SQL FROM SQLITE_MASTER WHERE TYPE='TABLE' "
        "AND NAME NOT IN (SELECT NAME FROM PRAGMA_TABLE_LIST WHERE SCHEMA='MAIN' AND TYPE='SHADOW')",
        "SELECT KEY, VALUE FROM"}
