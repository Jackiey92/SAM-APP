"""Pinned checks for F-001 per-component backup verification (design §10).

Covers the approved acceptance matrix: legal empty / non-empty /
chunk-spanning components verify against an independently recomputed
SHA-256; short, oversized, digest-mismatching and unreadable components
fail closed; invalid input (types, bool sizes/fds, non-UTF-8, NUL, bad
path segments, bad digests) is rejected before any component is opened;
closed or non-directory root descriptors, intermediate symlinks and
terminal symlinks / directories / FIFOs / sockets / devices are refused
without following links or opening non-regular files in data mode;
renaming the root or replacing pinned entries mid-walk still reads the
originally pinned inode, never a swapped-in substitute; missing Linux
capabilities or the /proc channel fail closed; and every failure
releases only the function's own descriptors while the caller's root fd
stays usable and the filesystem unchanged.

Only synthetic files under pytest's ``tmp_path`` are used. Device
behaviour is simulated by narrow ``os.fstat`` injection, never real
device I/O. Fault injection narrowly wraps ``os.open``/``os.read`` /
``os.fstat`` and delegates to the real functions; the SHA-256 always
runs over real bytes read from real files. No SAM runtime data, real
company data or network is touched.
"""
import hashlib
import os
import socket
import stat
import sys

import pytest

from app.facts_tree import backup_component_validation as validation_module
from app.facts_tree.backup_component_validation import (
    BackupComponentValidationError,
    VerifiedBackupComponent,
    verify_backup_component,
)

_ROOT_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CHUNK_SIZE = 1024 * 1024


def _open_root(path):
    return os.open(path, _ROOT_OPEN_FLAGS)


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_component(root, relative_path, data):
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _verify(root_fd, relative_path, data):
    return verify_backup_component(
        root_fd,
        relative_path=relative_path,
        size_bytes=len(data),
        sha256_digest=_digest(data),
    )


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


def _tree_listing(root):
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*")
    )


def test_empty_component_verifies(tmp_path):
    _write_component(tmp_path, "empty.bin", b"")
    root_fd = _open_root(tmp_path)
    try:
        result = _verify(root_fd, "empty.bin", b"")
    finally:
        os.close(root_fd)
    assert isinstance(result, VerifiedBackupComponent)
    assert result.relative_path == "empty.bin"
    assert result.size_bytes == 0
    assert result.sha256_digest == _digest(b"")
    assert (tmp_path / "empty.bin").read_bytes() == b""


def test_nested_component_verifies_and_leaves_tree_untouched(tmp_path):
    data = b"component-bytes" * 100
    target = _write_component(tmp_path, "sub/dir/blob.bin", data)
    before = _tree_listing(tmp_path)
    root_fd = _open_root(tmp_path)
    try:
        result = _verify(root_fd, "sub/dir/blob.bin", data)
    finally:
        os.close(root_fd)
    # Independent hashlib recomputation from the on-disk bytes.
    reread = target.read_bytes()
    assert result.size_bytes == len(data) == len(reread)
    assert result.sha256_digest == "sha256:" + hashlib.sha256(reread).hexdigest()
    assert reread == data
    assert _tree_listing(tmp_path) == before


def test_component_spanning_read_chunks_verifies(tmp_path, monkeypatch):
    data = bytes(range(256)) * 4096 + b"tail"  # 1 MiB + 4 bytes
    _write_component(tmp_path, "big.bin", data)
    read_calls = []
    real_read = os.read

    def counting_read(fd, count):
        chunk = real_read(fd, count)
        read_calls.append((count, len(chunk)))
        return chunk

    monkeypatch.setattr(os, "read", counting_read)
    root_fd = _open_root(tmp_path)
    try:
        result = _verify(root_fd, "big.bin", data)
    finally:
        os.close(root_fd)
    assert result.size_bytes == len(data)
    assert result.sha256_digest == _digest(data)
    # The stream really was chunked and bounded, and read exactly to EOF.
    assert len(read_calls) >= 2
    assert sum(length for _, length in read_calls) == len(data)
    assert all(count <= _CHUNK_SIZE for count, _ in read_calls)


def test_result_is_frozen(tmp_path):
    _write_component(tmp_path, "leaf", b"data")
    root_fd = _open_root(tmp_path)
    try:
        result = _verify(root_fd, "leaf", b"data")
    finally:
        os.close(root_fd)
    with pytest.raises(AttributeError):
        result.size_bytes = 0


def test_short_component_fails(tmp_path):
    data = b"short"
    _write_component(tmp_path, "c.bin", data)
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            verify_backup_component(
                root_fd,
                relative_path="c.bin",
                size_bytes=len(data) + 1,
                sha256_digest=_digest(data),
            )
    finally:
        os.close(root_fd)


def test_oversized_component_fails_and_read_is_bounded(tmp_path, monkeypatch):
    data = b"x" * 100
    declared = 50
    _write_component(tmp_path, "c.bin", data)
    read_calls = []
    real_read = os.read

    def counting_read(fd, count):
        chunk = real_read(fd, count)
        read_calls.append((count, len(chunk)))
        return chunk

    monkeypatch.setattr(os, "read", counting_read)
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            verify_backup_component(
                root_fd,
                relative_path="c.bin",
                size_bytes=declared,
                sha256_digest=_digest(data),
            )
    finally:
        os.close(root_fd)
    # Never more than the declared size plus one byte is pulled in.
    assert sum(length for _, length in read_calls) == declared + 1
    assert all(count <= _CHUNK_SIZE for count, _ in read_calls)


def test_digest_mismatch_fails(tmp_path):
    _write_component(tmp_path, "c.bin", b"real bytes")
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            verify_backup_component(
                root_fd,
                relative_path="c.bin",
                size_bytes=len(b"real bytes"),
                sha256_digest=_digest(b"other bytes"),
            )
    finally:
        os.close(root_fd)


def test_read_failure_fails_closed_and_keeps_caller_fd(tmp_path, monkeypatch):
    _write_component(tmp_path, "c.bin", b"payload")
    real_read = os.read

    def failing_read(fd, count):
        raise OSError("injected read failure")

    monkeypatch.setattr(os, "read", failing_read)
    root_fd = _open_root(tmp_path)
    before = _fd_count()
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "c.bin", b"payload")
        # Only the function's own descriptors are gone; the caller's
        # root fd is untouched and still usable.
        assert _fd_count() == before
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
    finally:
        os.close(root_fd)


def test_invalid_inputs_fail_before_any_open(tmp_path, monkeypatch):
    _write_component(tmp_path, "leaf", b"data")
    valid_digest = _digest(b"data")
    open_calls = []
    real_open = os.open

    def recording_open(*args, **kwargs):
        open_calls.append((args, kwargs))
        return real_open(*args, **kwargs)

    root_fd = _open_root(tmp_path)  # opened before recording starts
    monkeypatch.setattr(os, "open", recording_open)
    str_subclass = type("CustomStr", (str,), {})
    bad_overrides = [
        {"root_fd": True},
        {"root_fd": -1},
        {"root_fd": "3"},
        {"root_fd": 3.0},
        {"root_fd": None},
        {"relative_path": ""},
        {"relative_path": "/absolute"},
        {"relative_path": "a//b"},
        {"relative_path": "a/./b"},
        {"relative_path": "a/../b"},
        {"relative_path": ".."},
        {"relative_path": "."},
        {"relative_path": "leaf/"},
        {"relative_path": "a\\b"},
        {"relative_path": "a\x00b"},
        {"relative_path": "café\udcff"},
        {"relative_path": b"leaf"},
        {"relative_path": 123},
        {"relative_path": str_subclass("leaf")},
        {"size_bytes": True},
        {"size_bytes": -1},
        {"size_bytes": "4"},
        {"size_bytes": 4.0},
        {"sha256_digest": "sha256:" + "A" * 64},
        {"sha256_digest": "sha256:" + "0" * 63},
        {"sha256_digest": "sha256:" + "0" * 65},
        {"sha256_digest": "sha256:" + "g" * 64},
        {"sha256_digest": "0" * 64},
        {"sha256_digest": b"sha256:" + b"0" * 64},
        {"sha256_digest": str_subclass(valid_digest)},
        {"sha256_digest": 7},
    ]
    try:
        for overrides in bad_overrides:
            kwargs = {
                "root_fd": root_fd,
                "relative_path": "leaf",
                "size_bytes": 4,
                "sha256_digest": valid_digest,
            }
            kwargs.update(overrides)
            root_argument = kwargs.pop("root_fd")
            with pytest.raises(BackupComponentValidationError):
                verify_backup_component(root_argument, **kwargs)
    finally:
        os.close(root_fd)
    # Not a single invalid call reached as far as opening anything.
    assert open_calls == []


def test_closed_root_fd_fails(tmp_path):
    root_fd = _open_root(tmp_path)
    os.close(root_fd)
    with pytest.raises(BackupComponentValidationError):
        verify_backup_component(
            root_fd,
            relative_path="leaf",
            size_bytes=0,
            sha256_digest=_digest(b""),
        )


def test_non_directory_root_fd_fails(tmp_path):
    _write_component(tmp_path, "leaf", b"data")
    file_fd = os.open(tmp_path / "leaf", os.O_RDONLY)
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(file_fd, "leaf", b"data")
        # The caller's descriptor was not closed or damaged.
        assert stat.S_ISREG(os.fstat(file_fd).st_mode)
    finally:
        os.close(file_fd)


def test_intermediate_regular_file_fails(tmp_path):
    _write_component(tmp_path, "leaf", b"data")
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "leaf/inner", b"data")
    finally:
        os.close(root_fd)


def test_intermediate_symlink_is_never_followed(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "leaf").write_bytes(b"real-bytes")
    os.symlink("real", tmp_path / "link")
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "link/leaf", b"real-bytes")
    finally:
        os.close(root_fd)
    assert (real_dir / "leaf").read_bytes() == b"real-bytes"


def test_terminal_symlink_is_never_followed(tmp_path):
    (tmp_path / "target").write_bytes(b"target-bytes")
    os.symlink("target", tmp_path / "leaf")
    os.symlink("missing", tmp_path / "dangling")
    root_fd = _open_root(tmp_path)
    try:
        # Even a correct size/digest pair for the target content must
        # not let the symlink be followed.
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "leaf", b"target-bytes")
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "dangling", b"")
    finally:
        os.close(root_fd)
    assert (tmp_path / "target").read_bytes() == b"target-bytes"


def test_terminal_directory_fails(tmp_path):
    (tmp_path / "dir").mkdir()
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "dir", b"")
    finally:
        os.close(root_fd)


def test_terminal_fifo_fails_without_any_data_open(tmp_path, monkeypatch):
    os.mkfifo(tmp_path / "fifo")
    root_fd = _open_root(tmp_path)
    opens = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        opens.append((path, flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    try:
        # A data-mode open of a FIFO would block; the pinned O_PATH
        # check must refuse it before that can ever happen.
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "fifo", b"")
    finally:
        os.close(root_fd)
    assert opens
    assert all(flags & os.O_PATH for path, flags in opens if path == "fifo")
    assert not [
        path for path, _ in opens
        if isinstance(path, str) and path.startswith("/proc/self/fd/")
    ]


def test_terminal_socket_fails(tmp_path):
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(tmp_path / "sock"))
        root_fd = _open_root(tmp_path)
        try:
            with pytest.raises(BackupComponentValidationError):
                _verify(root_fd, "sock", b"")
        finally:
            os.close(root_fd)
    finally:
        listener.close()


def test_terminal_device_fails_via_fstat_injection(tmp_path, monkeypatch):
    _write_component(tmp_path, "dev", b"device-bytes")
    real_fstat = os.fstat

    def fstat_as_char_device(fd):
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            mode = stat.S_IFCHR | (result.st_mode & 0o7777)
            return os.stat_result((mode,) + tuple(result)[1:])
        return result

    monkeypatch.setattr(os, "fstat", fstat_as_char_device)
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "dev", b"device-bytes")
    finally:
        os.close(root_fd)


def test_renamed_root_still_reads_the_pinned_tree(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    data = b"original"
    _write_component(root, "sub/leaf", data)
    root_fd = _open_root(root)
    try:
        # Rename the root away and park an impostor tree at its old path.
        os.rename(root, tmp_path / "root-moved")
        impostor = tmp_path / "root"
        impostor.mkdir()
        _write_component(impostor, "sub/leaf", b"impostor")
        result = _verify(root_fd, "sub/leaf", data)
    finally:
        os.close(root_fd)
    assert result.sha256_digest == _digest(data)
    assert result.sha256_digest != _digest(b"impostor")


def test_intermediate_replaced_after_pin_reads_original(tmp_path, monkeypatch):
    data = b"original-intermediate"
    decoy = b"decoy-intermediate!!!"
    assert len(data) == len(decoy)
    _write_component(tmp_path, "sub/leaf", data)
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / "leaf").write_bytes(decoy)
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        # The terminal pin is the last descriptor-relative open of
        # "leaf"; by then the intermediate "sub" directory is pinned.
        if path == "leaf" and kwargs.get("dir_fd") is not None:
            os.rename(tmp_path / "sub", tmp_path / "sub-moved")
            os.symlink("decoy", tmp_path / "sub")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    root_fd = _open_root(tmp_path)
    try:
        result = _verify(root_fd, "sub/leaf", data)
    finally:
        os.close(root_fd)
    # The swapped-in symlink to the decoy tree is never followed.
    assert result.sha256_digest == _digest(data)
    assert result.sha256_digest != _digest(decoy)


def test_terminal_replaced_after_pin_reads_same_inode(tmp_path, monkeypatch):
    data = b"original-terminal"
    decoy = b"decoy-terminal-xx"
    assert len(data) == len(decoy)
    _write_component(tmp_path, "leaf", data)
    (tmp_path / "decoy").write_bytes(decoy)
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        # The /proc re-open happens after the terminal was pinned;
        # replacing the name must not change which inode is read.
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            os.unlink(tmp_path / "leaf")
            os.symlink("decoy", tmp_path / "leaf")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    root_fd = _open_root(tmp_path)
    try:
        result = _verify(root_fd, "leaf", data)
    finally:
        os.close(root_fd)
    assert result.sha256_digest == _digest(data)
    assert result.sha256_digest != _digest(decoy)


def test_missing_open_flag_capability_fails(tmp_path, monkeypatch):
    _write_component(tmp_path, "leaf", b"data")
    monkeypatch.delattr(os, "O_PATH")
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError, match="O_PATH"):
            _verify(root_fd, "leaf", b"data")
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
    finally:
        os.close(root_fd)


def test_missing_dir_fd_capability_fails(tmp_path, monkeypatch):
    _write_component(tmp_path, "leaf", b"data")
    monkeypatch.setattr(validation_module, "_OPEN_SUPPORTS_DIR_FD", False)
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError, match="dir_fd"):
            _verify(root_fd, "leaf", b"data")
    finally:
        os.close(root_fd)


def test_missing_proc_channel_fails(tmp_path, monkeypatch):
    _write_component(tmp_path, "leaf", b"data")
    monkeypatch.setattr(
        validation_module, "_PROC_SELF_FD", "/nonexistent-proc-self-fd"
    )
    root_fd = _open_root(tmp_path)
    try:
        with pytest.raises(BackupComponentValidationError, match="/proc"):
            _verify(root_fd, "leaf", b"data")
    finally:
        os.close(root_fd)


def test_open_failure_closes_only_own_fds(tmp_path, monkeypatch):
    data = b"data"
    _write_component(tmp_path, "sub/leaf", data)
    before_tree = _tree_listing(tmp_path)
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if path == "leaf":
            raise PermissionError("injected open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    root_fd = _open_root(tmp_path)
    before = _fd_count()
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "sub/leaf", data)
        assert _fd_count() == before
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
        assert _tree_listing(tmp_path) == before_tree
    finally:
        os.close(root_fd)


def test_successful_verification_leaks_no_fds(tmp_path):
    _write_component(tmp_path, "sub/leaf", b"data")
    root_fd = _open_root(tmp_path)
    before = _fd_count()
    try:
        _verify(root_fd, "sub/leaf", b"data")
        assert _fd_count() == before
    finally:
        os.close(root_fd)


def test_out_of_range_root_fd_and_unreprable_input_fail_closed(
    tmp_path, monkeypatch
):
    _write_component(tmp_path, "leaf", b"data")
    valid_digest = _digest(b"data")
    open_calls = []
    real_open = os.open

    def recording_open(*args, **kwargs):
        open_calls.append((args, kwargs))
        return real_open(*args, **kwargs)

    root_fd = _open_root(tmp_path)  # opened before recording starts
    monkeypatch.setattr(os, "open", recording_open)

    class BadRepr:
        def __repr__(self):
            raise RuntimeError("repr must not be called on rejected input")

    # A huge non-negative built-in int passes the type/sign check but is
    # outside the C int range os.dup converts into; the negative ones and
    # the arbitrary object must be rejected without repr'ing the value
    # (a huge int repr can exceed the int-to-str digit cap and a hostile
    # __repr__ can raise, either of which would leak the wrong error).
    bad_root_fds = [1 << 100, -(1 << 100), -(10 ** 5000), BadRepr()]
    try:
        for bad_root_fd in bad_root_fds:
            with pytest.raises(BackupComponentValidationError):
                verify_backup_component(
                    bad_root_fd,
                    relative_path="leaf",
                    size_bytes=4,
                    sha256_digest=valid_digest,
                )
        with pytest.raises(BackupComponentValidationError):
            verify_backup_component(
                root_fd,
                relative_path="leaf",
                size_bytes=-(10 ** 5000),
                sha256_digest=valid_digest,
            )
        # The caller's root fd was never touched by any of the rejections.
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
    finally:
        os.close(root_fd)
    # Every rejection happened before anything was opened.
    assert open_calls == []


def test_data_fd_close_error_still_releases_all_owned_fds(
    tmp_path, monkeypatch
):
    data = b"payload-close-error"
    _write_component(tmp_path, "sub/leaf", data)
    real_open = os.open
    real_close = os.close
    labels = {}
    closes = []

    def tracking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            labels[fd] = "data"
        return fd

    def close_with_data_failure(fd):
        # Linux can report a close error after the descriptor is already
        # released; simulate that window honestly: really close, then
        # raise. The verifier must not retry the close (the number could
        # have been reused) and must still release everything else.
        real_close(fd)
        closes.append(fd)
        if labels.get(fd) == "data":
            raise OSError("injected close failure after real close")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", close_with_data_failure)
    root_fd = _open_root(tmp_path)
    before = _fd_count()
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "sub/leaf", data)
        # The pin, the intermediate directory, the duplicated root and
        # the (really closed) data descriptor are all gone; the caller's
        # root fd is untouched and still usable.
        assert _fd_count() == before
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
    finally:
        os.close(root_fd)
    # No descriptor was closed twice.
    assert len(closes) == len(set(closes))
    assert any(labels.get(fd) == "data" for fd in closes)


def test_dir_close_error_still_releases_later_owned_fds(
    tmp_path, monkeypatch
):
    data = b"nested-close-error"
    _write_component(tmp_path, "a/b/leaf", data)
    real_open = os.open
    real_close = os.close
    labels = {}
    closes = []

    def tracking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == "a" and kwargs.get("dir_fd") is not None:
            labels[fd] = "first-dir"
        return fd

    def close_with_dir_failure(fd):
        real_close(fd)
        closes.append(fd)
        if labels.get(fd) == "first-dir":
            raise OSError("injected close failure after real close")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", close_with_dir_failure)
    root_fd = _open_root(tmp_path)
    before = _fd_count()
    try:
        with pytest.raises(BackupComponentValidationError):
            _verify(root_fd, "a/b/leaf", data)
        # The unified cleanup still released the second directory, the
        # pin, the data descriptor and the duplicated root even though
        # the first directory's close reported an error.
        assert _fd_count() == before
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
    finally:
        os.close(root_fd)
    assert len(closes) == len(set(closes))
    assert any(labels.get(fd) == "first-dir" for fd in closes)


def test_huge_declared_size_mismatch_reports_defined_error(tmp_path):
    data = b"tiny"
    _write_component(tmp_path, "leaf", data)
    root_fd = _open_root(tmp_path)
    before = _fd_count()
    # Tighten the int-to-str digit cap for real so that formatting the
    # declared size into an error message would raise a built-in
    # ValueError; the huge int is built inside the body so no collection
    # or parameter name ever has to repr it.
    old_cap = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        huge_size = 10 ** 5000
        with pytest.raises(BackupComponentValidationError):
            verify_backup_component(
                root_fd,
                relative_path="leaf",
                size_bytes=huge_size,
                sha256_digest=_digest(data),
            )
        # The short file was really read to EOF and the mismatch is the
        # defined error; the caller's root fd stays usable and nothing
        # leaked.
        assert _fd_count() == before
        assert stat.S_ISDIR(os.fstat(root_fd).st_mode)
    finally:
        sys.set_int_max_str_digits(old_cap)
        os.close(root_fd)
