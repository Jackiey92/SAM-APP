"""Pinned checks for the F-001 single-SQLite checkpoint artifact.

Covers the per-database W checkpoint primitive of the approved F-001
backup design sections 3 and 5: committed WAL frames that have not been
checkpointed into the main DB file are captured and verified, integrity
/ foreign-key verification failures are never published, existing
destinations (including symlinks) are never overwritten or followed,
invalid sources and missing directories fail closed, Backup API and
verification errors leave no artifact behind, and the published
file's bytes match the returned size and sha256 digest.

Only synthetic databases under pytest's ``tmp_path`` are used; no SAM
runtime data, real company data or network is touched.
"""
import hashlib
import os
import sqlite3
import stat

import pytest

from app.facts_tree import backup_sqlite_checkpoint as checkpoint_module
from app.facts_tree.backup_sqlite_checkpoint import (
    DIGEST_ALGORITHM,
    SqliteCheckpointError,
    create_sqlite_checkpoint,
)


def _make_wal_source(directory, *, name="src.db"):
    """A WAL database with committed, still-un-checkpointed data."""
    source_path = directory / name
    connection = sqlite3.connect(source_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute(
        "CREATE TABLE child ("
        "id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id))"
    )
    connection.execute("INSERT INTO parent VALUES (1, 'alice')")
    connection.execute("INSERT INTO child VALUES (10, 1)")
    connection.commit()
    return connection, source_path


def _assert_no_temp_artifacts(directory):
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert not [path for path in files if path.name.startswith(".sqlite-checkpoint-")]
    assert not [path for path in files if path.name.endswith(".tmp")]
    assert not [path for path in files if path.name.endswith(".tmp-wal")]
    assert not [path for path in files if path.name.endswith(".tmp-shm")]
    assert not [path for path in files if path.name.endswith(".tmp-journal")]


def test_wal_frames_not_in_main_db_are_captured(tmp_path):
    connection, source_path = _make_wal_source(tmp_path)
    # The committed row still lives only in the WAL: the frames exist
    # beside a main DB file that does not contain the payload bytes.
    wal_path = tmp_path / "src.db-wal"
    assert wal_path.exists() and wal_path.stat().st_size > 0
    assert b"alice" not in source_path.read_bytes()

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    destination = output_dir / "app.db-checkpoint"
    result = create_sqlite_checkpoint(connection, destination)

    # Source ownership is untouched: still open, idle, WAL mode, data intact.
    assert connection.in_transaction is False
    assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert connection.execute("SELECT name FROM parent").fetchall() == [("alice",)]
    connection.close()

    # The artifact is exactly one self-contained file in delete mode.
    assert sorted(path.name for path in output_dir.iterdir()) == ["app.db-checkpoint"]
    assert result.path == destination
    payload = destination.read_bytes()
    assert result.size_bytes == len(payload) == destination.stat().st_size
    assert result.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert result.digest == f"{DIGEST_ALGORITHM}:" + hashlib.sha256(payload).hexdigest()
    assert len(result.digest) == len("sha256:") + 64

    # The copied database reads the un-checkpointed data and passes both
    # checks independently of the producing connection.
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert restored.execute("SELECT name FROM parent").fetchall() == [("alice",)]
        assert restored.execute("SELECT pid FROM child").fetchall() == [(1,)]
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        restored.close()
    # Opening the artifact for verification created no sidecars.
    assert sorted(path.name for path in output_dir.iterdir()) == ["app.db-checkpoint"]


def test_result_is_immutable(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    result = create_sqlite_checkpoint(connection, tmp_path / "cp.db")
    with pytest.raises(AttributeError):
        result.size_bytes = 0
    with pytest.raises(AttributeError):
        result.digest = "sha256:" + "0" * 64
    connection.close()


def test_foreign_key_violation_fails_and_cleans_temp(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    # Sneak an orphan child row in with foreign-key enforcement off.
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("INSERT INTO child VALUES (11, 999)")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA foreign_key_check").fetchall()

    destination = tmp_path / "cp.db"
    with pytest.raises(SqliteCheckpointError, match="foreign_key_check"):
        create_sqlite_checkpoint(connection, destination)

    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    # The source connection was never closed or otherwise disturbed.
    assert connection.execute("SELECT COUNT(*) FROM child").fetchone() == (2,)
    connection.close()


def test_corrupt_source_fails_verification_without_publishing(tmp_path):
    source_path = tmp_path / "bad.db"
    writer = sqlite3.connect(source_path)
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    writer.executemany(
        "INSERT INTO t VALUES (?, ?)",
        [(index, "z" * 200) for index in range(300)],
    )
    writer.commit()
    writer.close()

    size = source_path.stat().st_size
    with open(source_path, "r+b") as handle:
        handle.seek(size // 2)
        handle.write(b"\xff" * 1024)

    connection = sqlite3.connect(source_path)
    destination = tmp_path / "cp.db"
    # Corruption either makes integrity_check return non-'ok' rows or make
    # the pragma raise outright; both are verification failures.
    with pytest.raises(SqliteCheckpointError, match="integrity_check|verification"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_existing_destination_file_is_never_overwritten(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    destination.write_bytes(b"ORIGINAL-CONTENT")
    original_stat = destination.stat()

    with pytest.raises(SqliteCheckpointError, match="already exists"):
        create_sqlite_checkpoint(connection, destination)

    assert destination.read_bytes() == b"ORIGINAL-CONTENT"
    assert destination.stat().st_mtime_ns == original_stat.st_mtime_ns
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_existing_destination_symlink_is_never_followed(tmp_path):
    connection, _ = _make_wal_source(tmp_path)

    keeper = tmp_path / "keeper"
    keeper.write_bytes(b"KEEP-ME-UNTOUCHED")
    link = tmp_path / "cp.db"
    link.symlink_to(keeper)

    with pytest.raises(SqliteCheckpointError, match="already exists"):
        create_sqlite_checkpoint(connection, link)
    assert keeper.read_bytes() == b"KEEP-ME-UNTOUCHED"
    assert link.is_symlink()
    assert os.readlink(link) == str(keeper)

    # A dangling symlink blocks publication as well and stays a symlink.
    dangling = tmp_path / "cp2.db"
    dangling.symlink_to(tmp_path / "does-not-exist")
    with pytest.raises(SqliteCheckpointError, match="already exists"):
        create_sqlite_checkpoint(connection, dangling)
    assert dangling.is_symlink()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_non_connection_source_is_rejected(tmp_path):
    destination = tmp_path / "cp.db"
    for bad in (None, 42, "not-a-connection", object()):
        with pytest.raises(SqliteCheckpointError, match="sqlite3.Connection"):
            create_sqlite_checkpoint(bad, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)


def test_closed_connection_is_rejected(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.close()
    destination = tmp_path / "cp.db"
    with pytest.raises(SqliteCheckpointError, match="closed or unusable"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)


def test_source_with_unfinished_transaction_is_rejected(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    connection.execute("INSERT INTO parent VALUES (2, 'bob')")
    assert connection.in_transaction

    destination = tmp_path / "cp.db"
    with pytest.raises(SqliteCheckpointError, match="unfinished transaction"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)

    connection.rollback()
    connection.close()


def test_missing_destination_directory_fails_closed(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "missing-dir" / "cp.db"
    with pytest.raises(SqliteCheckpointError, match="directory"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.parent.exists()
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_invalid_destination_type_and_empty_path_fail_closed(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    with pytest.raises(SqliteCheckpointError, match="str or os.PathLike"):
        create_sqlite_checkpoint(connection, 12345)
    with pytest.raises(SqliteCheckpointError):
        create_sqlite_checkpoint(connection, "")
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_backup_api_error_publishes_nothing(tmp_path):
    # The immutable built-in sqlite3.Connection cannot be monkeypatched; a
    # connection factory subclass lets us make backup() itself fail.
    class ExplodingConnection(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            raise sqlite3.DatabaseError("simulated Backup API failure")

    connection = sqlite3.connect(tmp_path / "boom.db", factory=ExplodingConnection)
    connection.execute("CREATE TABLE t (a)")
    connection.commit()

    destination = tmp_path / "cp.db"
    with pytest.raises(SqliteCheckpointError, match="SQLite Backup API failed"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_publish_race_target_is_never_overwritten(tmp_path, monkeypatch):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"

    def racing_hard_link(source, target, *args, **kwargs):
        raise FileExistsError(17, "simulated destination appearance")

    monkeypatch.setattr(checkpoint_module.os, "link", racing_hard_link)

    with pytest.raises(SqliteCheckpointError, match="appeared while"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_unsupported_hard_link_fails_without_overwrite(tmp_path, monkeypatch):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"

    def unsupported_hard_link(source, target, *args, **kwargs):
        raise OSError(1, "simulated operation not permitted")

    monkeypatch.setattr(checkpoint_module.os, "link", unsupported_hard_link)

    with pytest.raises(SqliteCheckpointError, match="unsupported"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_directory_fsync_failure_after_link_removes_only_our_inode(
    tmp_path, monkeypatch
):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    real_fsync = os.fsync

    def fail_on_directories(file_descriptor):
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError(5, "simulated directory fsync failure")
        return real_fsync(file_descriptor)

    monkeypatch.setattr(checkpoint_module.os, "fsync", fail_on_directories)

    with pytest.raises(SqliteCheckpointError, match="destination directory"):
        create_sqlite_checkpoint(connection, destination)
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()
