"""D14-4 sessions over genuine jointly published, generation-pinned databases."""
import ast
from dataclasses import FrozenInstanceError, fields, replace
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from unittest.mock import Mock

import pytest

from app.facts_tree import projection_bundle as bundles, projection_router as router
from app.facts_tree import projection_read_session as sessions, projection_status as status
from app.facts_tree import projection_derived_query as derived, projection_query as query
from tests.facts_tree_full_fixture import SCOPE
from tests.test_projection_advance_service import _run
from tests.test_projection_bundle import assert_closed, track_connections, TrackedConnection
from tests.test_projection_incremental import chain  # noqa: F401


@pytest.fixture(autouse=True)
def configured_key():
    query.set_cursor_key(b"test-only-D4-host-signing-key")


@pytest.fixture
def published(chain, tmp_path):
    repo, commits = chain
    root, directory = tmp_path / "root", tmp_path / "generations"
    _run(repo, root, directory, target=commits[1])
    return root, directory, repo, commits


def inventory(root, directory):
    return {str(path): sha256(path.read_bytes()).hexdigest()
            for base in (root, directory) for path in base.rglob("*") if path.is_file()}


def damage_derived(root, damage):
    path = Path(bundles.derived_path(router.load_current(root, **SCOPE)))
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"not a sqlite database")


def test_complete_readonly_session_and_queries(published, monkeypatch):
    root, directory, _, commits = published
    expected = router.load_current(root, **SCOPE)
    before = inventory(root, directory)
    opened = track_connections(monkeypatch)
    bundle_close = Mock(wraps=bundles.ProjectionBundle.close)
    # Track the ownership method while preserving its bound receiver.
    def close_bundle(handle):
        bundle_close(handle)
    monkeypatch.setattr(bundles.ProjectionBundle, "close", close_bundle)
    with sessions.open_read_session(root, **SCOPE) as session:
        assert type(session) is sessions.ReadSession and session.derived_ready
        assert session.serving == expected == session.bundle.serving
        assert session.standard is session.bundle.standard
        assert session.derived is session.bundle.derived
        assert len(opened) == 3  # load verification, standard, derived; no second open_current.
        assert [f.name for f in fields(session) if f.init] == ["serving", "standard", "derived", "bundle"]
        with pytest.raises(FrozenInstanceError):
            session.serving = None
        for connection in (session.standard, session.derived):
            assert connection.total_changes == 0
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("CREATE TABLE forbidden(x)")
        canonical = query.list_records(session.standard, authz_digest="scope", limit=1)
        wide = derived.list_family_rows(session.bundle, "fact_revision", authz_digest="scope", limit=1)
        search = derived.search(session.bundle, "示例", authz_digest="scope", family="fact_revision")
        assert canonical.records and canonical.next_cursor
        assert wide.rows and wide.next_cursor and search.hits
        assert wide.projected_commit == search.projected_commit == commits[1]
    session.close()
    bundle_close.assert_called_once_with(session.bundle)
    assert_closed(opened)
    assert inventory(root, directory) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_degraded_session_and_strict_failure(published, monkeypatch, damage):
    root, directory, _, _ = published
    damage_derived(root, damage)
    before = inventory(root, directory)
    opened = track_connections(monkeypatch)
    with sessions.open_read_session(root, **SCOPE) as session:
        assert not session.derived_ready and session.derived is None and session.bundle is None
        assert session.standard.execute("SELECT count(*) FROM record_canonical").fetchone()[0] > 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            session.standard.execute("DELETE FROM generation_meta")
    session.close()
    assert_closed(opened)
    with pytest.raises(bundles.BundleUnavailable):
        sessions.open_read_session(root, **SCOPE, allow_degraded=False)
    assert_closed(opened)
    assert inventory(root, directory) == before


@pytest.mark.parametrize("allow_degraded", [True, False])
def test_no_current_and_broken_standard_never_degrade(published, tmp_path, monkeypatch, allow_degraded):
    root = published[0]
    with pytest.raises(router.NoCurrentProjection):
        sessions.open_read_session(tmp_path / "empty", **SCOPE, allow_degraded=allow_degraded)
    serving = router.load_current(root, **SCOPE)
    Path(serving.db_path).unlink()
    fallback = Mock(side_effect=AssertionError("must not attempt fallback"))
    # The standard failure propagates from the bundle before any fallback.
    original = sessions.open_bundle
    error = None
    try:
        original(root, **SCOPE)
    except router.CurrentProjectionUnavailable as exc:
        error = exc
    assert error is not None
    monkeypatch.setattr(sessions, "open_bundle", Mock(side_effect=error))
    monkeypatch.setattr(router, "open_current", fallback)
    with pytest.raises(router.CurrentProjectionUnavailable) as caught:
        sessions.open_read_session(root, **SCOPE, allow_degraded=allow_degraded)
    assert caught.value is error
    fallback.assert_not_called()


@pytest.mark.parametrize("degraded", [False, True])
def test_status_truth_counts_and_no_mutation(published, degraded):
    root, directory, repo, commits = published
    if degraded:
        damage_derived(root, "missing")
    before = inventory(root, directory)
    with sessions.open_read_session(root, **SCOPE) as session:
        original = session.serving
        count = session.standard.execute("SELECT count(*) FROM record_canonical").fetchone()[0]
        active = session.standard.execute("SELECT count(*) FROM fact_current_view WHERE is_active=1").fetchone()[0]
        for head, snapshot, pending, snapshot_current, current in [
            (commits[1], commits[1], False, True, True),
            (commits[2], commits[2], True, True, False),
            (repo.head_oid(), commits[1], True, False, False),
            (commits[1], commits[2], False, False, True),
            (commits[1], None, False, None, True),
        ]:
            envelope = sessions.session_status(session, head, snapshot_commit=snapshot)
            assert tuple(i.name for i in envelope.indexes) == sessions.REQUIRED_INDEXES == (
                "canonical_records", "wide_records", "fts_records")
            assert tuple(i.ready for i in envelope.indexes) == (True, not degraded, not degraded)
            assert (envelope.projection_pending, envelope.snapshot_current, envelope.serving_current) == (
                pending, snapshot_current, current)
            assert envelope.management_ready == (not degraded and current and snapshot_current is True)
            assert envelope.generation_record_count == original.record_count == count
            assert envelope.generation_active_fact_count == original.active_fact_count == active
            assert (envelope.repo_id, envelope.company_id, envelope.generation_id,
                    envelope.serving_commit, envelope.built_against_head, envelope.object_format) == (
                original.repo_id, original.company_id, original.generation_id,
                original.projected_commit, original.head_oid, original.object_format)
        assert session.serving is original and original.required_indexes == ("canonical_records",)
    assert inventory(root, directory) == before


def test_advance_requires_reopen_and_old_cursors_remain_pinned(published):
    root, directory, repo, commits = published
    with sessions.open_read_session(root, **SCOPE) as old:
        first = derived.list_family_rows(old.bundle, "fact_revision", authz_digest="scope", limit=1)
        canonical = query.list_records(old.standard, authz_digest="scope", limit=1)
        assert first.next_cursor and canonical.next_cursor
        old_serving = old.serving
        _run(repo, root, directory, target=commits[2])
        with sessions.open_read_session(root, **SCOPE) as new:
            assert old.serving is old_serving
            assert old.serving.projected_commit == commits[1]
            assert new.serving.projected_commit == commits[2]
            assert old.serving.generation_id != new.serving.generation_id
            assert derived.list_family_rows(old.bundle, "fact_revision", authz_digest="scope", limit=1,
                                            cursor=first.next_cursor).projected_commit == commits[1]
            assert query.list_records(old.standard, authz_digest="scope", limit=1,
                                      cursor=canonical.next_cursor).projected_commit == commits[1]
            with pytest.raises(query.SnapshotExpired, match="SNAPSHOT_EXPIRED"):
                derived.list_family_rows(new.bundle, "fact_revision", authz_digest="scope", limit=1,
                                         cursor=first.next_cursor)
            with pytest.raises(query.SnapshotExpired, match="SNAPSHOT_EXPIRED"):
                query.list_records(new.standard, authz_digest="scope", limit=1, cursor=canonical.next_cursor)
            assert sessions.session_status(old, commits[2]).projection_pending
            assert not sessions.session_status(new, commits[2]).projection_pending


@pytest.mark.parametrize("degraded", [False, True])
def test_context_body_exception_is_preserved_and_all_owners_close(published, monkeypatch, degraded):
    root = published[0]
    if degraded:
        damage_derived(root, "missing")
    opened = track_connections(monkeypatch)
    error = RuntimeError("body failure")
    with pytest.raises(RuntimeError) as caught:
        with sessions.open_read_session(root, **SCOPE) as session:
            raise error
    assert caught.value is error
    session.close()
    assert_closed(opened)


def test_derived_verification_failure_closes_pair_before_fallback(published, monkeypatch):
    opened = track_connections(monkeypatch)
    error = RuntimeError("derived verification failure")
    monkeypatch.setattr(bundles, "_verify_derived", Mock(side_effect=error))
    with sessions.open_read_session(published[0], **SCOPE) as session:
        assert not session.derived_ready
        assert len(opened) == 5
        assert_closed(opened[:4])
        assert session.standard is opened[-1]
        assert session.standard.execute("SELECT 1").fetchone() == (1,)
    assert_closed(opened)


@pytest.mark.parametrize("degraded", [False, True])
def test_constructor_failure_returns_no_leaked_handle(published, monkeypatch, degraded):
    if degraded:
        damage_derived(published[0], "missing")
    opened = track_connections(monkeypatch)
    error = RuntimeError("session constructor failure")
    monkeypatch.setattr(sessions, "ReadSession", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        sessions.open_read_session(published[0], **SCOPE)
    assert caught.value is error
    assert_closed(opened)


def test_bundle_close_failure_still_closes_derived_and_is_idempotent(published, monkeypatch):
    opened = track_connections(monkeypatch)
    session = sessions.open_read_session(published[0], **SCOPE)
    original_close = TrackedConnection.close
    error = RuntimeError("standard close failure")

    def close(connection):
        original_close(connection)
        if connection is session.standard:
            raise error

    monkeypatch.setattr(TrackedConnection, "close", close)
    with pytest.raises(RuntimeError) as caught:
        session.close()
    assert caught.value is error
    session.close()
    assert_closed(opened)


def test_error_identities_and_fallback_failure(published, monkeypatch):
    derived_error = bundles.BundleUnavailable("derived")
    monkeypatch.setattr(sessions, "open_bundle", Mock(side_effect=derived_error))
    fallback = Mock(side_effect=router.CurrentProjectionUnavailable("standard changed"))
    monkeypatch.setattr(router, "open_current", fallback)
    with pytest.raises(bundles.BundleUnavailable) as caught:
        sessions.open_read_session(published[0], **SCOPE, allow_degraded=False)
    assert caught.value is derived_error
    fallback.assert_not_called()
    for error in (router.NoCurrentProjection("no current"), router.CurrentProjectionUnavailable("changed")):
        fallback.side_effect = error
        with pytest.raises(type(error)) as caught:
            sessions.open_read_session(published[0], **SCOPE)
        assert caught.value is error


def test_guards_and_d5_error_passthrough(published, monkeypatch):
    for value in (None, 0, 1, "true", [], object()):
        with pytest.raises(TypeError, match="allow_degraded must be bool"):
            sessions.open_read_session(published[0], **SCOPE, allow_degraded=value)
    for value in (None, object(), published[0]):
        with pytest.raises(status.ProjectionStatusError, match="^session must be a ReadSession$"):
            sessions.session_status(value, "a" * 40)
    with sessions.open_read_session(published[0], **SCOPE) as session:
        with pytest.raises(TypeError):
            sessions.session_status(session)
        for name in ("head_commit", "snapshot_commit"):
            for value in ("A" * 40, "a" * 39, "a" * 41, "a" * 64, "g" * 40, "", 1, False):
                kwargs = {"head_commit": session.serving.projected_commit, name: value}
                with pytest.raises(status.ProjectionStatusError) as caught:
                    sessions.session_status(session, **kwargs)
                with pytest.raises(status.ProjectionStatusError) as direct:
                    status.build_projection_status(serving=session.serving, **kwargs)
                assert str(caught.value) == str(direct.value)
        with pytest.raises(status.ProjectionStatusError):
            sessions.session_status(session, None)
        # Only the view's object format/OIDs change; storage remains the real chain's.
        sha256_view = replace(session, serving=replace(session.serving, object_format="sha256",
                              projected_commit="a" * 64, head_oid="b" * 64))
        assert sessions.session_status(sha256_view, "a" * 64, snapshot_commit="a" * 64).management_ready
        with pytest.raises(status.ProjectionStatusError):
            sessions.session_status(sha256_view, "a" * 40)
        error = status.ProjectionStatusError("D5 sentinel")
        monkeypatch.setattr(sessions, "build_projection_status", Mock(side_effect=error))
        with pytest.raises(status.ProjectionStatusError) as caught:
            sessions.session_status(session, session.serving.projected_commit)
        assert caught.value is error


def test_scope_validation_is_delegated_without_opening(tmp_path):
    for kwargs in ({"repo_id": None}, {"company_id": ""}, {"root": 1}):
        inputs = {"root": tmp_path / "empty", **SCOPE, **kwargs}
        with pytest.raises((ValueError, TypeError)) as direct:
            router.open_current(**inputs)
        with pytest.raises(type(direct.value)) as caught:
            sessions.open_read_session(**inputs)
        assert str(caught.value) == str(direct.value)


def test_source_imports_readonly_boundaries_and_no_direct_connection():
    source = Path(sessions.__file__).read_text()
    tree = ast.parse(source)
    allowed = {"contextlib": {"ExitStack"}, "dataclasses": {"dataclass", "field", "replace"},
               "app.facts_tree": {"projection_router"},
               "app.facts_tree.projection_bundle": {"BundleUnavailable", "ProjectionBundle", "open_bundle"},
               "app.facts_tree.projection_status": {"ProjectionStatus", "ProjectionStatusError", "build_projection_status"}}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {alias.name for alias in node.names} <= {"sqlite3"}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
            assert {alias.name for alias in node.names} <= allowed[node.module]
        elif isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            assert not name.endswith((".connect", ".execute", ".executescript", ".executemany", ".commit", ".rollback"))
            if name.startswith("router."):
                assert name == "router.open_current"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ATTACH|PRAGMA|REPLACE INTO)\b", node.value, re.I)
    assert sessions.ReadSession.__dataclass_params__.frozen
