"""Pinned checks for the F-001 single-SQLite checkpoint artifact.

Covers the per-database W checkpoint primitive of the approved F-001
backup design sections 3 and 5: committed WAL frames that have not been
checkpointed into the main DB file are captured and verified, integrity
/ foreign-key verification failures are never published, existing
destinations (including symlinks) are never overwritten or followed,
invalid sources and missing directories fail closed, Backup API and
verification errors leave no artifact behind, and the published
file's bytes match the returned size and sha256 digest.

Hardening coverage: the destination directory is pinned by descriptor
and must be a non-group/world-writable directory owned by the current
user, staging is a private 0700 directory with a 0600 snapshot reached
through a descriptor-pinned /proc/self/fd path that survives concurrent
directory-entry replacement, bytes paths are rejected before any
artifact exists, and every failure reports a truthful publication state
(absent / confirmed / uncertain), including deterministic injection of
the directory-sync, rollback-unlink and post-publication cleanup paths.

Only synthetic databases under pytest's ``tmp_path`` are used; no SAM
runtime data, real company data or network is touched.
"""
import hashlib
import os
import shutil
import sqlite3
import stat

import pytest

from app.facts_tree import backup_sqlite_checkpoint as checkpoint_module
from app.facts_tree.backup_sqlite_checkpoint import (
    DIGEST_ALGORITHM,
    PublicationState,
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
    entries = list(directory.rglob("*"))
    files = [path for path in entries if path.is_file()]
    assert not [
        path
        for path in entries
        if path.name.startswith(".sqlite-checkpoint-stage-")
    ]
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
    with pytest.raises(SqliteCheckpointError, match="foreign_key_check") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    assert exc_info.value.publication == PublicationState.ABSENT
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

    with pytest.raises(SqliteCheckpointError, match="already exists") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    assert exc_info.value.publication == PublicationState.ABSENT
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

    with pytest.raises(SqliteCheckpointError, match="appeared while") as exc_info:
        create_sqlite_checkpoint(connection, destination)
    assert exc_info.value.publication == PublicationState.ABSENT
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_unsupported_hard_link_fails_without_overwrite(tmp_path, monkeypatch):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"

    def unsupported_hard_link(source, target, *args, **kwargs):
        raise OSError(1, "simulated operation not permitted")

    monkeypatch.setattr(checkpoint_module.os, "link", unsupported_hard_link)

    with pytest.raises(SqliteCheckpointError, match="unsupported") as exc_info:
        create_sqlite_checkpoint(connection, destination)
    assert exc_info.value.publication == PublicationState.ABSENT
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def _directory_fsync_fail_on_calls(failing_calls, monkeypatch):
    """Patch os.fsync so the Nth directory-fsync call(s) fail."""
    real_fsync = os.fsync
    state = {"directory_calls": 0}

    def patched(file_descriptor):
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            state["directory_calls"] += 1
            if state["directory_calls"] in failing_calls:
                raise OSError(5, "simulated directory fsync failure")
        return real_fsync(file_descriptor)

    monkeypatch.setattr(checkpoint_module.os, "fsync", patched)


def test_publish_fsync_failure_with_synced_rollback_reports_absent(
    tmp_path, monkeypatch
):
    # The publication directory sync fails once; the inode-guarded rollback
    # unlink and its own directory sync succeed, so absence is durable.
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    _directory_fsync_fail_on_calls({1}, monkeypatch)

    with pytest.raises(SqliteCheckpointError, match="rolled the link back") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    assert exc_info.value.publication == PublicationState.ABSENT
    assert exc_info.value.result is None
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_publish_fsync_failure_without_rollback_sync_reports_uncertain(
    tmp_path, monkeypatch
):
    # Every directory fsync fails: the in-memory link can be unlinked, but
    # without a synced rollback durability of the absence cannot be promised.
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    real_fsync = os.fsync

    def fail_on_directories(file_descriptor):
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError(5, "simulated directory fsync failure")
        return real_fsync(file_descriptor)

    monkeypatch.setattr(checkpoint_module.os, "fsync", fail_on_directories)

    with pytest.raises(SqliteCheckpointError, match="may exist") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    assert exc_info.value.publication == PublicationState.UNCERTAIN
    assert not destination.exists()  # the rollback unlink itself worked
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_publish_fsync_failure_with_blocked_rollback_unlink_is_uncertain(
    tmp_path, monkeypatch
):
    # Directory sync fails AND the rollback unlink of the destination fails:
    # the destination truly remains and the state is uncertain, never
    # reported as a clean absence.
    connection, _ = _make_wal_source(tmp_path)
    output_dir = tmp_path
    destination = output_dir / "cp.db"
    parent_stat = os.stat(output_dir)
    real_fsync = os.fsync
    real_unlink = os.unlink

    def fail_on_directories(file_descriptor):
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError(5, "simulated directory fsync failure")
        return real_fsync(file_descriptor)

    def block_destination_unlink(name, *args, dir_fd=None, **kwargs):
        if (
            dir_fd is not None
            and os.fstat(dir_fd).st_ino == parent_stat.st_ino
            and name == destination.name
        ):
            raise OSError(1, "simulated rollback unlink failure")
        return real_unlink(name, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(checkpoint_module.os, "fsync", fail_on_directories)
    monkeypatch.setattr(checkpoint_module.os, "unlink", block_destination_unlink)

    with pytest.raises(SqliteCheckpointError, match="may exist") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    assert exc_info.value.publication == PublicationState.UNCERTAIN
    # The surviving link is exactly the checkpoint bytes this call produced.
    assert destination.exists()
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("SELECT name FROM parent").fetchall() == [("alice",)]
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        restored.close()
    connection.close()


def test_confirmed_publication_survives_staging_unlink_failure(tmp_path, monkeypatch):
    # After the destination link and its directory fsync have succeeded the
    # checkpoint is CONFIRMED; a failure removing the private snapshot does
    # not roll the destination back and comes back with a usable result.
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    real_unlink = os.unlink

    def block_snapshot_unlink(name, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and os.fstat(dir_fd).st_mode & stat.S_IFDIR:
            if isinstance(name, str) and name.endswith(".tmp"):
                raise OSError(1, "simulated snapshot removal failure")
        return real_unlink(name, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(checkpoint_module.os, "unlink", block_snapshot_unlink)

    with pytest.raises(SqliteCheckpointError, match="staging cleanup") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    error = exc_info.value
    assert error.publication == PublicationState.CONFIRMED
    result = error.result
    assert result is not None and result.path == destination
    payload = destination.read_bytes()
    assert result.size_bytes == len(payload)
    assert result.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("SELECT name FROM parent").fetchall() == [("alice",)]
    finally:
        restored.close()

    # The only leftover is this call's private 0700 staging directory with a
    # 0600 snapshot inside; clean it as the operator would after inspection.
    stage_dirs = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".sqlite-checkpoint-stage-")
    ]
    assert len(stage_dirs) == 1
    assert stat.S_IMODE(stage_dirs[0].stat().st_mode) == 0o700
    leftovers = list(stage_dirs[0].iterdir())
    assert len(leftovers) == 1
    assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
    # Operator-side cleanup after inspection; stop injecting failures.
    monkeypatch.undo()
    shutil.rmtree(stage_dirs[0])
    connection.close()


def test_confirmed_publication_survives_final_directory_sync_failure(
    tmp_path, monkeypatch
):
    # Directory fsyncs: #1 publishes the link (must succeed), the staging
    # directory sync succeeds, #2 (after staging rmdir) fails. The
    # destination was already durable after #1, so this stays CONFIRMED.
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    _directory_fsync_fail_on_calls({3}, monkeypatch)

    with pytest.raises(SqliteCheckpointError, match="staging cleanup") as exc_info:
        create_sqlite_checkpoint(connection, destination)

    assert exc_info.value.publication == PublicationState.CONFIRMED
    result = exc_info.value.result
    payload = destination.read_bytes()
    assert result.size_bytes == len(payload)
    assert result.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert not [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".sqlite-checkpoint-stage-")
    ]
    connection.close()


def test_group_and_world_writable_directories_are_rejected_before_artifacts(
    tmp_path,
):
    connection, _ = _make_wal_source(tmp_path)
    for mode in (0o777, 0o775, 0o757, 0o1777):
        output_dir = tmp_path / f"out-{mode:o}"
        os.mkdir(output_dir, mode)
        os.chmod(output_dir, mode)
        destination = output_dir / "cp.db"
        with pytest.raises(SqliteCheckpointError, match="writable") as exc_info:
            create_sqlite_checkpoint(connection, destination)
        assert exc_info.value.publication == PublicationState.ABSENT
        assert os.listdir(output_dir) == []
    connection.close()


def test_foreign_owned_directory_is_rejected_before_artifacts(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    try:
        os.chown(output_dir, 65534, 65534)
    except (PermissionError, OSError):
        pytest.skip("needs privilege to create a directory owned by another uid")
    try:
        connection, _ = _make_wal_source(tmp_path)
        with pytest.raises(SqliteCheckpointError, match="owned by the current"):
            create_sqlite_checkpoint(connection, output_dir / "cp.db")
        assert os.listdir(output_dir) == []
        connection.close()
    finally:
        os.chown(output_dir, os.geteuid(), os.getegid())


def test_symlinked_destination_directory_is_rejected(tmp_path):
    connection, _ = _make_wal_source(tmp_path)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir)
    with pytest.raises(SqliteCheckpointError, match="symlink"):
        create_sqlite_checkpoint(connection, linked_dir / "cp.db")
    assert os.listdir(real_dir) == []
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_private_staging_is_0700_with_0600_snapshot_and_is_cleaned_on_failure(
    tmp_path, monkeypatch
):
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    observed = {}

    def inspect_then_fail(source, staging):
        stage_stat = os.fstat(staging.dir_fd)
        entries = os.listdir(staging.dir_fd)
        temp_fd = os.open(
            staging.temp_name,
            os.O_PATH | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=staging.dir_fd,
        )
        try:
            temp_stat = os.fstat(temp_fd)
        finally:
            os.close(temp_fd)
        observed["stage_mode"] = stat.S_IMODE(stage_stat.st_mode)
        observed["stage_uid"] = stage_stat.st_uid
        observed["entries"] = entries
        observed["temp_mode"] = stat.S_IMODE(temp_stat.st_mode)
        observed["temp_uid"] = temp_stat.st_uid
        raise SqliteCheckpointError("injected staging failure")

    monkeypatch.setattr(checkpoint_module, "_backup_and_verify", inspect_then_fail)

    with pytest.raises(SqliteCheckpointError, match="injected staging failure") as exc_info:
        create_sqlite_checkpoint(connection, destination)
    assert exc_info.value.publication == PublicationState.ABSENT
    assert observed["stage_mode"] == 0o700
    assert observed["stage_uid"] == os.geteuid()
    assert len(observed["entries"]) == 1
    assert observed["entries"][0].startswith(".sqlite-checkpoint-")
    assert observed["entries"][0].endswith(".tmp")
    assert observed["temp_mode"] == 0o600
    assert observed["temp_uid"] == os.geteuid()
    assert not destination.exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


def test_pinned_staging_defeats_concurrent_directory_entry_replacement(
    tmp_path, monkeypatch
):
    # Simulate an actor who can replace the private staging directory entry
    # right when SQLite opens the snapshot (possible only if the documented
    # ownership/permission precondition is violated). SQLite is handed a
    # /proc/self/fd-pinned path, so every byte still lands in OUR directory
    # and the impostor's file can never be published.
    connection, _ = _make_wal_source(tmp_path)
    destination = tmp_path / "cp.db"
    real_connect = sqlite3.connect
    swapped = {"done": False}

    def swap_on_snapshot_open(path, *args, **kwargs):
        if (
            not swapped["done"]
            and isinstance(path, str)
            and path.startswith("/proc/self/fd/")
        ):
            swapped["done"] = True
            dir_fd = int(path.split("/")[4])
            stage_path = os.readlink(f"/proc/self/fd/{dir_fd}")
            temp_name = path.rsplit("/", 1)[1]
            moved_aside = tmp_path / "stolen-stage"
            os.rename(stage_path, moved_aside)
            impostor = tmp_path / os.path.basename(stage_path)
            os.mkdir(impostor, 0o777)
            with open(impostor / temp_name, "wb") as handle:
                handle.write(b"EVIL-BYTES" * 8)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(checkpoint_module.sqlite3, "connect", swap_on_snapshot_open)

    # Publication itself is intact; only staging removal reports a (confirmed)
    # cleanup problem because the name now points at the impostor directory.
    try:
        result = create_sqlite_checkpoint(connection, destination)
    except SqliteCheckpointError as exc:
        assert exc.publication == PublicationState.CONFIRMED
        assert "staging" in str(exc)
        result = exc.result

    # The published bytes come from the pinned (renamed) staging, never from
    # the impostor directory entry.
    payload = destination.read_bytes()
    assert b"EVIL-BYTES" not in payload
    assert result.size_bytes == len(payload)
    assert result.digest == "sha256:" + hashlib.sha256(payload).hexdigest()
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("SELECT name FROM parent").fetchall() == [("alice",)]
    finally:
        restored.close()
    impostors = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".sqlite-checkpoint-stage-")
    ]
    assert len(impostors) == 1
    assert (impostors[0] / os.listdir(impostors[0])[0]).read_bytes().startswith(
        b"EVIL-BYTES"
    )
    # Our own renamed staging directory had its snapshot unlinked via fd.
    assert os.listdir(tmp_path / "stolen-stage") == []
    shutil.rmtree(impostors[0])
    shutil.rmtree(tmp_path / "stolen-stage")
    connection.close()


def test_bytes_destination_paths_are_rejected_before_any_artifact(tmp_path):
    connection, _ = _make_wal_source(tmp_path)

    class BytesLike:
        def __fspath__(self):
            return b"cp.db"

    with pytest.raises(SqliteCheckpointError, match="bytes") as exc_info:
        create_sqlite_checkpoint(connection, str(tmp_path).encode() + b"/cp.db")
    assert exc_info.value.publication == PublicationState.ABSENT
    with pytest.raises(SqliteCheckpointError, match="bytes"):
        create_sqlite_checkpoint(connection, BytesLike())
    assert not (tmp_path / "cp.db").exists()
    _assert_no_temp_artifacts(tmp_path)
    connection.close()


@pytest.mark.parametrize("bad_name_suffix", ["/", "/cp.db/.."])
def test_destination_must_name_a_file_inside_a_directory(tmp_path, bad_name_suffix):
    connection, _ = _make_wal_source(tmp_path)
    with pytest.raises(SqliteCheckpointError, match="file inside a directory"):
        create_sqlite_checkpoint(connection, str(tmp_path) + bad_name_suffix)
    _assert_no_temp_artifacts(tmp_path)
    connection.close()
