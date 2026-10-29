"""Offline installer tests: synthetic archives, signatures and Node probes only."""
import base64
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from app import managed_runtime_installer as installer
from app.runtime_config import RuntimeConfig
from app.runtime_contract import RuntimeContractError, load_contract, preflight_runtime
from tests.runtime_install import NODE_BYTES, ROOT, managed_runner


def forbidden(*args, **kwargs):
    pytest.fail("installer test attempted network or a real subprocess/Node execution")


REAL_DOWNLOAD = installer.download
REAL_VERIFY_NODE_SIGNATURE = installer.verify_node_signature
PINNED_NODE_SIGNER = "5BE8A3F6C8A5C01D106C0AD820B1A390B168D356"


@pytest.fixture(autouse=True)
def no_network_or_execution(monkeypatch):
    monkeypatch.setattr(installer, "download", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def archive(path, members):
    with tarfile.open(path, "w:gz") as output:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if isinstance(data, tuple):
                info.type, info.linkname = data
                output.addfile(info)
            else:
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))


def mock_clearsigned_gpg(monkeypatch, payload, *, fingerprint=PINNED_NODE_SIGNER):
    """Emit synthetic GPG status/payload; never execute GPG or retrieve keys."""
    assert installer.NODE_RELEASE_FINGERPRINT == PINNED_NODE_SIGNER
    calls = []
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/fake/gpg" if name == "gpg" else None)

    def gpg(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["/fake/gpg", "--no-options", "--batch"]
        assert "--no-auto-key-retrieve" in argv and "--homedir" in argv
        if "--import" in argv:
            assert kwargs["input"] == b"synthetic public key"
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert "--decrypt" in argv and "--status-fd" in argv
        Path(argv[argv.index("--output") + 1]).write_bytes(payload)
        status = f"[GNUPG:] VALIDSIG {fingerprint} 2026-10-29 0 0 4 0 22 8 01\n".encode()
        return subprocess.CompletedProcess(argv, 0, status, b"")

    monkeypatch.setattr(subprocess, "run", gpg)
    return calls


def clearsign_inputs(directory, checksums):
    txt = directory / "SHASUMS256.txt"
    txt.write_bytes(checksums)
    asc = directory / "SHASUMS256.txt.asc"
    asc.write_bytes(b"-----BEGIN PGP SIGNED MESSAGE-----\nsynthetic signature")
    key = directory / "release-key.asc"
    key.write_bytes(b"synthetic public key")
    return txt, asc, key


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    (project / "sam-manifest.yaml").write_bytes((ROOT / "sam-manifest.yaml").read_bytes())
    pins = load_contract(project, env={})
    source = tmp_path / "artifacts"
    source.mkdir()
    oc = source / Path(pins.openclaw_tarball).name
    node_dir = f"node-v{pins.node_version}-{pins.node_platform}-{pins.node_arch}"
    node = source / f"{node_dir}.tar.gz"
    oc_members = [("package/openclaw.mjs", b"synthetic entry; never execute", 0o644),
                  ("package/package.json", json.dumps({"version": pins.openclaw_version}).encode(), 0o644),
                  ("package/dist/owned.js", b"synthetic dependency", 0o644)]
    node_members = [(f"{node_dir}/bin/node", NODE_BYTES, 0o755),
                    (f"{node_dir}/lib/npm.js", b"synthetic npm; never execute", 0o644),
                    (f"{node_dir}/bin/npm", (tarfile.SYMTYPE, "../lib/npm.js"), 0o777)]
    archive(oc, oc_members)
    archive(node, node_members)
    original_hash = installer._hash
    oc_hash = original_hash(oc, "sha512")

    def synthetic_artifact_hash(path, algorithm):
        # Hash actual bytes. Substitute only the exact synthetic artifact's
        # digest; any tampering escapes the seam and fails the SRI comparison.
        digest = original_hash(path, algorithm)
        if algorithm == "sha512" and digest == oc_hash:
            return base64.b64decode(pins.tarball_sha512_b64)
        return digest

    node_digest = original_hash(node, "sha256").hex()
    monkeypatch.setattr(installer, "_hash", synthetic_artifact_hash)
    monkeypatch.setattr(installer, "NODE_TAR_GZ_SHA256", node_digest)
    checksums = source / "SHASUMS256.txt"
    checksums.write_text(f"{node_digest}  {node.name}\n", encoding="ascii")
    (source / "SHASUMS256.txt.asc").write_bytes(b"synthetic signature; not real evidence")
    signature_calls = []

    def signature(checksums, asc, work, key):
        signature_calls.append((checksums, asc))
        assert checksums.read_bytes() == (source / checksums.name).read_bytes()
        assert asc.read_bytes() == (source / asc.name).read_bytes()
        return f"synthetic GPG signed SHASUMS; {installer.NODE_RELEASE_FINGERPRINT}"

    monkeypatch.setattr(installer, "verify_node_signature", signature)
    return SimpleNamespace(project=project, source=source, pins=pins, oc=oc, node=node,
                           oc_members=oc_members, node_members=node_members, node_dir=node_dir,
                           signature_calls=signature_calls, checksums=checksums)


def run_install(a, **kwargs):
    return installer.install(a.project, staging_dir=a.source, env={}, runner=managed_runner(), **kwargs)


def proof(a):
    config = RuntimeConfig.from_env(a.project, env={})
    return preflight_runtime(config, env={}, runner=managed_runner())


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


def test_offline_happy_path_all_payloads_covered(artifacts):
    a = artifacts
    root = run_install(a)
    assert proof(a).openclaw_commit == a.pins.openclaw_commit
    ledger = json.loads((root / "install-ledger.json").read_text())
    assert set(ledger) == {"schema", "contract", "files", "openclaw", "node"}
    assert ledger["schema"] == 1
    payload = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert {r["path"] for r in ledger["files"]} == payload - {"install-ledger.json"}
    assert ledger["node"]["binary"] == f"{a.node_dir}/bin/node"
    assert ledger["node"]["sha256"] == hashlib.sha256(NODE_BYTES).hexdigest()
    assert ledger["node"]["official_sha256"] == installer.NODE_TAR_GZ_SHA256
    assert ledger["openclaw"]["tarball_sha512"] == a.pins.tarball_sha512_b64
    provenance = ledger["openclaw"]["commit_provenance"]
    for value in (a.pins.openclaw_commit, installer.OPENCLAW_TAG_OBJECT,
                  installer.OPENCLAW_BUILD_COMMIT, "attestations/openclaw@", "slsa.dev/provenance/v1"):
        assert value in provenance
    info = (root / "install-ledger.json").stat()
    assert info.st_mode & 0o777 == 0o600
    assert info.st_nlink == 1 and info.st_uid == os.getuid()
    assert root.stat().st_mode & 0o022 == 0
    assert all(p.resolve().is_relative_to(root) for p in root.rglob("*") if p.is_symlink())
    assert (root / a.node_dir / "bin/npm").is_symlink()
    assert os.readlink(root / a.node_dir / "bin/npm") == "../lib/npm.js"
    assert (root / a.node_dir / "bin/npm").read_bytes() == b"synthetic npm; never execute"
    assert len(a.signature_calls) == 1
    assert sorted(p.name for p in (root.parent / ".oc-runtime.installer").iterdir()) == ["install.lock"]


def test_idempotent_rebuild(artifacts):
    root = run_install(artifacts)
    before = snapshot(root)
    assert run_install(artifacts) == root
    assert snapshot(root) == before
    assert len(artifacts.signature_calls) == 2
    assert proof(artifacts).node_version == artifacts.pins.node_version


@pytest.mark.parametrize("which", ["openclaw", "node", "signed_line", "missing"])
def test_artifact_failure_leaves_no_live_root(artifacts, which):
    a = artifacts
    if which == "openclaw":
        a.oc.write_bytes(a.oc.read_bytes() + b"tamper")
        message = "SRI mismatch"
    elif which == "node":
        a.node.write_bytes(a.node.read_bytes() + b"tamper")
        message = "Node tarball SHA-256 mismatch"
    elif which == "signed_line":
        a.checksums.write_text(f"{'0' * 64}  {a.node.name}\n")
        message = "signed SHASUMS line"
    else:
        a.node.unlink()
        message = "offline artifact is missing"
    with pytest.raises(installer.InstallError, match=message):
        run_install(a)
    assert not (a.project / ".oc-runtime").exists()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["extract", "preflight", "publish", "signature"])
def test_failure_preserves_old_root(artifacts, monkeypatch, existing, failure):
    a = artifacts
    root = a.project / ".oc-runtime"
    if existing:
        run_install(a)
    before = snapshot(root) if existing else None
    if failure == "preflight":
        archive(a.node, [(f"{a.node_dir}/bin/not-node", NODE_BYTES, 0o755)])
        digest = hashlib.sha256(a.node.read_bytes()).hexdigest()
        monkeypatch.setattr(installer, "NODE_TAR_GZ_SHA256", digest)
        a.checksums.write_text(f"{digest}  {a.node.name}\n")
    else:
        def fail(*args, **kwargs):
            if failure == "extract":
                args[1].mkdir(parents=True, exist_ok=True)
                (args[1] / "partial").write_bytes(b"partial staging only")
            raise installer.InstallError(f"synthetic {failure} failure")
        monkeypatch.setattr(installer, {"extract": "extract_archive", "publish": "publish",
                                       "signature": "verify_node_signature"}[failure], fail)
    with pytest.raises((installer.InstallError, OSError)):
        run_install(a)
    if existing:
        assert snapshot(root) == before
        assert proof(a).openclaw_version == a.pins.openclaw_version
    else:
        assert not root.exists()
    assert not list((a.project / ".oc-runtime.installer").glob("stage-*"))


@pytest.mark.parametrize("url", [
    "http://registry.npmjs.org/openclaw/-/openclaw.tgz", "https://evil.test/openclaw.tgz",
    "https://registry.npmjs.org.evil.test/openclaw.tgz", "https://user@registry.npmjs.org/openclaw.tgz",
    "https://registry.npmjs.org/openclaw/-/openclaw-latest.tgz", "https://nodejs.org:444/dist/file",
    "https://nodejs.org/dist/file?redirect=evil", "https://nodejs.org/dist/file#fragment",
])
def test_nonofficial_or_tampered_url_refused(url):
    with pytest.raises(installer.InstallError, match="exact official HTTPS"):
        installer._official_url(url, {load_contract(ROOT, env={}).openclaw_tarball})


@pytest.mark.parametrize("name,data", [
    ("../escape", b"bad"), ("/tmp/escape", b"bad"), ("package/../../escape", b"bad"),
    ("other/file", b"bad"), ("package/back\\slash", b"bad"),
    ("package/link", (tarfile.SYMTYPE, "../../escape")),
    ("package/link", (tarfile.SYMTYPE, "/tmp/escape")),
    ("package/link", (tarfile.LNKTYPE, "../escape")),
    ("package/link", (tarfile.SYMTYPE, "link")),
    ("package/fifo", (tarfile.FIFOTYPE, "")),
])
@pytest.mark.parametrize("node_archive", [False, True])
def test_unsafe_archive_rejected(tmp_path, name, data, node_archive):
    prefix = "node-dist" if node_archive else "package"
    name = name.replace("package", prefix)
    path = tmp_path / "unsafe.tgz"
    archive(path, [(name, data, 0o755)])
    with pytest.raises(installer.InstallError):
        installer.extract_archive(path, tmp_path / "runtime", prefix)
    assert not (tmp_path / "escape").exists()


def test_archive_duplicate_file_refused(tmp_path):
    path = tmp_path / "duplicate.tgz"
    archive(path, [("package/x", b"a", 0o644), ("package/x", b"b", 0o644)])
    with pytest.raises(installer.InstallError, match="duplicate"):
        installer.extract_archive(path, tmp_path / "runtime", "package")


@pytest.mark.parametrize("section,key,value", [
    (None, "schema", 2), ("contract", "commit", "0" * 40),
    ("contract", "openclaw", "latest"), ("contract", "integrity", "sha512-wrong"),
    ("openclaw", "commit", "0" * 40), ("openclaw", "tarball_sha512", "wrong"),
    ("node", "binary", "../escape"), ("files", "path", "../escape"),
    ("files", "path", "/tmp/escape"),
])
def test_unchanged_gate_rejects_installer_ledger_drift(artifacts, section, key, value):
    root = run_install(artifacts)
    path = root / "install-ledger.json"
    ledger = json.loads(path.read_text())
    target = ledger if section is None else ledger[section][0] if section == "files" else ledger[section]
    target[key] = value
    path.write_text(json.dumps(ledger))
    with pytest.raises(RuntimeContractError):
        proof(artifacts)


def test_every_deployed_dependency_is_checked(artifacts):
    root = run_install(artifacts)
    (root / "node_modules/openclaw/dist/owned.js").write_bytes(b"tamper")
    with pytest.raises(RuntimeContractError, match="file digest mismatch"):
        proof(artifacts)


def test_retargeted_payload_symlink_is_checked(artifacts):
    root = run_install(artifacts)
    link = root / artifacts.node_dir / "bin/npm"
    link.unlink()
    link.symlink_to("node")
    with pytest.raises(RuntimeContractError, match="file digest mismatch"):
        proof(artifacts)


def test_bad_package_version_fails_original_gate_before_publish(artifacts, monkeypatch):
    a = artifacts
    root = run_install(a)
    before = snapshot(root)
    archive(a.oc, [("package/openclaw.mjs", b"synthetic entry", 0o644),
                   ("package/package.json", b'{"version":"latest"}', 0o644)])
    actual_hash = installer._hash
    monkeypatch.setattr(installer, "_hash", lambda p, algorithm: (
        base64.b64decode(a.pins.tarball_sha512_b64) if algorithm == "sha512" else actual_hash(p, algorithm)))
    with pytest.raises(RuntimeContractError, match="package version must be exactly"):
        run_install(a)
    assert snapshot(root) == before
    assert proof(a).openclaw_version == a.pins.openclaw_version


def test_bad_node_metadata_fails_original_gate_before_publish(artifacts):
    a = artifacts
    def wrong_node(argv, **kwargs):
        assert Path(argv[0]).read_bytes() == NODE_BYTES
        return subprocess.CompletedProcess(argv, 0, "v0.0.0\n", "")
    with pytest.raises(RuntimeContractError, match="must report exactly"):
        installer.install(a.project, staging_dir=a.source, env={}, runner=wrong_node)
    assert not (a.project / ".oc-runtime").exists()


@pytest.mark.parametrize("env", [
    {"SAM_CLOUD_READONLY": "1"}, {"SAM_NODE_BIN": "/usr/bin/node"},
    {"SAM_OPENCLAW_ENTRY": "elsewhere.mjs"},
])
def test_config_drift_or_cloud_refused_before_staging(artifacts, env):
    with pytest.raises(installer.InstallError):
        installer.install(artifacts.project, env=env, staging_dir=artifacts.source, runner=forbidden)
    assert not (artifacts.project / ".oc-runtime.installer").exists()


def test_profile_cannot_relax_pins(artifacts):
    profile = artifacts.project / "drift.yaml"
    profile.write_text("runtime:\n  node: latest\n")
    with pytest.raises(installer.InstallError, match="drifts"):
        installer.install(artifacts.project, env={"SAM_PROFILE_PATH": str(profile)}, runner=forbidden)


def test_manifest_drift_refused(artifacts):
    manifest = artifacts.project / "sam-manifest.yaml"
    manifest.write_text(manifest.read_text().replace(artifacts.pins.openclaw_version, "latest"))
    with pytest.raises(RuntimeContractError):
        run_install(artifacts)
    assert not (artifacts.project / ".oc-runtime.installer").exists()


def _contending_install(a, connection):
    try:
        run_install(a, lock_timeout=0.15)
    except installer.InstallError as exc:
        connection.send(str(exc))
    else:
        connection.send("unexpected install")
    finally:
        connection.close()


def test_cross_process_lock_blocks_staging_then_releases(artifacts, monkeypatch):
    a = artifacts
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    root = a.project / ".oc-runtime"
    with installer.install_lock(root):
        with monkeypatch.context() as patch:
            patch.setattr(installer, "extract_archive", forbidden)
            process = ctx.Process(target=_contending_install, args=(a, child))
            process.start()
            assert parent.poll(5)
            assert "timed out" in parent.recv()
            process.join(5)
            assert process.exitcode == 0
        assert not root.exists()
        assert not list((a.project / ".oc-runtime.installer").glob("stage-*"))
    parent.close()
    child.close()
    run_install(a)
    assert proof(a).node_version == a.pins.node_version


def _die_holding_lock(root, connection):
    with installer.install_lock(root):
        connection.send("locked")
        os._exit(0)


def test_lock_holder_death_releases_flock(tmp_path):
    parent, child = multiprocessing.get_context("fork").Pipe()
    root = tmp_path / "runtime"
    process = multiprocessing.get_context("fork").Process(target=_die_holding_lock, args=(root, child))
    process.start()
    assert parent.poll(5) and parent.recv() == "locked"
    process.join(5)
    assert process.exitcode == 0
    with installer.install_lock(root, timeout=0):
        pass
    parent.close()
    child.close()


@pytest.mark.parametrize("target", ["root", "state", "lock"])
def test_symlink_install_destinations_refused(artifacts, target):
    a = artifacts
    outside = a.project / "outside"
    outside.mkdir()
    state = a.project / ".oc-runtime.installer"
    if target == "root":
        (a.project / ".oc-runtime").symlink_to(outside)
    elif target == "state":
        state.symlink_to(outside)
    else:
        state.mkdir(mode=0o700)
        (state / "install.lock").symlink_to(outside / "lock")
    with pytest.raises((installer.InstallError, OSError)):
        run_install(a)
    assert list(outside.iterdir()) == []


def test_atomic_exchange_syscall_failure_preserves_both(tmp_path, monkeypatch):
    staged, live = tmp_path / "staged", tmp_path / "live"
    staged.mkdir(mode=0o700)
    live.mkdir(mode=0o700)
    (live / "old").write_bytes(b"old")
    (staged / "new").write_bytes(b"new")
    class Exchange:
        def __call__(self, *args):
            return -1
    monkeypatch.setattr(installer.ctypes, "CDLL", lambda *a, **k: SimpleNamespace(renameat2=Exchange()))
    with pytest.raises(installer.InstallError, match="existing runtime left intact"):
        installer.publish(staged, live)
    assert (live / "old").read_bytes() == b"old"
    assert (staged / "new").read_bytes() == b"new"


def load_cli():
    spec = importlib.util.spec_from_file_location("install_managed_runtime_cli", ROOT / "scripts/install-managed-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_failure_is_clear_nonzero(monkeypatch, capsys):
    module = load_cli()
    def fail(*args, **kwargs):
        raise installer.InstallError("GnuPG unavailable; refusing install")
    monkeypatch.setattr(module, "install", fail)
    assert module.main([]) == 1
    assert "GnuPG unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("explicit_key", [False, True])
@pytest.mark.parametrize("failure", [None, "missing_gpg", "missing_key", "import", "bad_sig",
                                    "other_signer", "unsigned_txt", "no_validsig", "not_clearsigned"])
def test_gpg_fails_closed_and_binds_txt(tmp_path, monkeypatch, failure, explicit_key):
    # Exercise the real verifier, separately from the synthetic artifact seam.
    txt = tmp_path / "SHASUMS256.txt"
    asc = tmp_path / "SHASUMS256.txt.asc"
    txt.write_bytes(b"synthetic checksums\n")
    asc.write_bytes(b"-----BEGIN PGP SIGNED MESSAGE-----\nsynthetic signature")
    if failure == "not_clearsigned":
        asc.write_bytes(b"unsigned message")
    key = tmp_path / "release-key.asc"
    key.write_bytes(b"public key")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None if failure == "missing_gpg" else "/fake/gpg")
    calls = []
    def gpg(argv, **kwargs):
        calls.append(argv)
        assert argv[1:3] == ["--no-options", "--batch"]
        if "--export" in argv:
            return subprocess.CompletedProcess(argv, 0, b"" if failure == "missing_key" else b"public key", b"")
        assert "--no-auto-key-retrieve" in argv
        if "--import" in argv:
            assert kwargs["input"] == b"public key"
            return subprocess.CompletedProcess(argv, int(failure == "import"), b"", b"")
        assert "--decrypt" in argv
        Path(argv[argv.index("--output") + 1]).write_bytes(b"different\n" if failure == "unsigned_txt" else txt.read_bytes())
        fingerprint = "0" * 40 if failure == "other_signer" else installer.NODE_RELEASE_FINGERPRINT
        status = b"" if failure == "no_validsig" else f"[GNUPG:] VALIDSIG {fingerprint} 2026-10-29 0 0 4 0 22 8 01\n".encode()
        return subprocess.CompletedProcess(argv, int(failure == "bad_sig"), status, b"")
    monkeypatch.setattr(subprocess, "run", gpg)
    if failure and not (failure == "missing_key" and explicit_key):
        with pytest.raises(installer.InstallError):
            installer.verify_node_signature(txt, asc, tmp_path, key if explicit_key else None)
    else:
        assert installer.NODE_RELEASE_FINGERPRINT in installer.verify_node_signature(txt, asc, tmp_path, key if explicit_key else None)
        assert len(calls) == (2 if explicit_key else 3)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\n\n", b"\n \t\n\t \n", b"\r\n\r\n \t\r\n"])
@pytest.mark.parametrize("blank_lines_in_verified", [False, True])
def test_clearsign_trailing_blank_lines_install_and_preflight_pass(
        artifacts, tmp_path, monkeypatch, ending, blank_lines_in_verified):
    a = artifacts
    # Mirror the reviewer's 34-line payload; only its EOF representation varies.
    original = a.checksums.read_bytes() + b"".join(
        f"{'f' * 64}  synthetic-other-{index}.tar.gz\n".encode() for index in range(33))
    changed = original.rstrip(b"\n") + ending
    standalone, authenticated = (original, changed) if blank_lines_in_verified else (changed, original)
    a.checksums.write_bytes(standalone)
    (a.source / "SHASUMS256.txt.asc").write_bytes(
        b"-----BEGIN PGP SIGNED MESSAGE-----\nsynthetic signature")
    key = tmp_path / "release-key.asc"
    key.write_bytes(b"synthetic public key")
    calls = mock_clearsigned_gpg(monkeypatch, authenticated)
    monkeypatch.setattr(installer, "verify_node_signature", REAL_VERIFY_NODE_SIGNATURE)

    root = run_install(a, node_release_key=key)
    assert proof(a).openclaw_commit == a.pins.openclaw_commit
    ledger = json.loads((root / "install-ledger.json").read_text())
    assert PINNED_NODE_SIGNER in ledger["node"]["official_signature"]
    assert len(calls) == 2


@pytest.mark.parametrize("tampering", [
    "hash_character", "filename_character", "insert_entry", "remove_entry", "remove_last_entry",
    "append_entry", "reorder", "leading_blank", "leading_bytes", "internal_blank",
    "internal_whitespace", "embedded_cr", "embedded_crlf", "content_trailing_space",
    "last_content_trailing_space",
])
def test_gpg_checksum_content_tampering_is_rejected(tmp_path, monkeypatch, tampering):
    lines = [f"{letter * 64}  node-{letter}.tar.gz".encode() for letter in "abc"]
    standalone = b"\n".join(lines) + b"\n"
    extra = b"d" * 64 + b"  inserted.tar.gz"
    changed = {
        "hash_character": b"d" + standalone[1:],
        "filename_character": standalone.replace(b"node-b", b"node-X"),
        "insert_entry": b"\n".join([lines[0], extra, *lines[1:]]) + b"\n",
        "remove_entry": b"\n".join([lines[0], lines[2]]) + b"\n",
        "remove_last_entry": b"\n".join(lines[:-1]) + b"\n",
        "append_entry": standalone + extra + b"\n",
        "reorder": b"\n".join(reversed(lines)) + b"\n",
        "leading_blank": b"\n" + standalone,
        "leading_bytes": b" " + standalone,
        "internal_blank": b"\n".join([lines[0], b"", *lines[1:]]) + b"\n",
        "internal_whitespace": b"\n".join([lines[0], b" \t", *lines[1:]]) + b"\n",
        "embedded_cr": standalone.replace(b"node-b", b"node-\rb"),
        "embedded_crlf": standalone.replace(b"node-b", b"node-\r\nb"),
        "content_trailing_space": b"\n".join([lines[0] + b" ", *lines[1:]]) + b"\n",
        "last_content_trailing_space": standalone[:-1] + b" \n",
    }[tampering]
    txt, asc, key = clearsign_inputs(tmp_path, standalone)
    mock_clearsigned_gpg(monkeypatch, changed)
    with pytest.raises(installer.InstallError, match="differs from the GPG-authenticated"):
        installer.verify_node_signature(txt, asc, tmp_path, key)


def test_clearsign_trailing_blanks_do_not_allow_wrong_fingerprint(tmp_path, monkeypatch):
    txt, asc, key = clearsign_inputs(tmp_path, b"synthetic checksum line\n")
    mock_clearsigned_gpg(monkeypatch, txt.read_bytes() + b"\n \t\n", fingerprint="0" * 40)
    with pytest.raises(installer.InstallError, match="signer is not the pinned fingerprint"):
        installer.verify_node_signature(txt, asc, tmp_path, key)


def test_online_downloader_proxy_and_redirect_policy(tmp_path, monkeypatch):
    url = load_contract(ROOT, env={}).openclaw_tarball
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/fake/curl")
    def curl(argv, **kwargs):
        assert argv[1] == "-q" and "--location" not in argv
        assert argv[argv.index("--proto") + 1] == "=https"
        assert argv[argv.index("--max-redirs") + 1] == "0"
        assert "env" not in kwargs  # inherit standard proxy environment
        return subprocess.CompletedProcess(argv, 0, b"302", b"")
    monkeypatch.setattr(subprocess, "run", curl)
    with pytest.raises(installer.InstallError, match="redirects refused"):
        REAL_DOWNLOAD(url, tmp_path / "artifact", {url})


def test_online_artifacts_use_same_validation_path(artifacts, monkeypatch):
    a = artifacts
    urls = []
    def synthetic_download(url, target, allowed):
        installer._official_url(url, allowed)
        assert target.parent.parent.stat().st_mode & 0o777 == 0o700
        urls.append(url)
        target.write_bytes((a.source / target.name).read_bytes())
    monkeypatch.setattr(installer, "download", synthetic_download)
    installer.install(a.project, env={}, runner=managed_runner())
    assert len(urls) == 4 and a.pins.openclaw_tarball in urls
    assert len(a.signature_calls) == 1
    assert proof(a).openclaw_commit == a.pins.openclaw_commit


def test_streaming_hash_multiple_chunks(tmp_path):
    path = tmp_path / "large"
    data = b"x" * ((1 << 20) + 17)
    path.write_bytes(data)
    assert installer._hash(path, "sha512") == hashlib.sha512(data).digest()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_lock_timeout_refused(tmp_path, timeout):
    with pytest.raises(installer.InstallError, match="lock timeout"):
        with installer.install_lock(tmp_path / "runtime", timeout):
            pytest.fail("invalid lock timeout was accepted")
