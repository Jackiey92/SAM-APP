"""One-shot, verified installation of the manifest's managed runtime.

No npm lifecycle scripts, dependency resolution, gateway or update commands run.
Only GnuPG, curl (online mode) and preflight's Node metadata probes execute.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

from .runtime_config import RuntimeConfig, _read_config
from .runtime_contract import (
    ENTRY_SUFFIX, LEDGER_NAME, load_contract, preflight_runtime,
)


class InstallError(RuntimeError):
    """Installation failed closed; the previous runtime remains published."""


# Public evidence supplied by the reviewer on 2026-10-29, not a version source.
NODE_RELEASE_FINGERPRINT = "5BE8A3F6C8A5C01D106C0AD820B1A390B168D356"
NODE_TAR_GZ_SHA256 = "eb6728e77c576a35162c122c45ccadf224d89ff00abc8dae46cc2897c6002d7c"
OPENCLAW_TAG_OBJECT = "5c101ce5a0637df375c5f6ae49d44d0d27cc26b1"
OPENCLAW_BUILD_COMMIT = "723d9c20878e8ecadb684883854b9707d5c31050"


def _hash(path: Path, algorithm: str) -> bytes:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.digest()


def _official_url(url: str, allowed: set[str]) -> None:
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.netloc in {"nodejs.org", "registry.npmjs.org"}
                 and not parts.query and not parts.fragment and url in allowed)
    except ValueError:
        valid = False
    if not valid:
        raise InstallError("artifact URL must match the exact official HTTPS URL")


def download(url: str, destination: Path, allowed: set[str]) -> None:
    """curl supports HTTPS_PROXY/https_proxy/ALL_PROXY, including SOCKS proxies.

    Refuse redirects entirely, before another host can receive a request.
    -q disables user curlrc, which could otherwise override TLS/redirect policy.
    """
    _official_url(url, allowed)
    curl = shutil.which("curl")
    if not curl:
        raise InstallError("online installation requires curl; use --staging-dir for offline artifacts")
    result = subprocess.run(
        [curl, "-q", "--fail", "--silent", "--show-error", "--proto", "=https",
         "--max-redirs", "0", "--max-time", "300", "--output", str(destination),
         "--write-out", "%{http_code}", url], capture_output=True, timeout=310,
    )
    if result.returncode != 0 or result.stdout.strip() != b"200":
        # Do not echo proxy environment/credentials or untrusted server messages.
        raise InstallError(f"official HTTPS download failed (curl exit {result.returncode}); redirects refused")


def verify_node_signature(checksums: Path, signature: Path, work: Path,
                          release_key: Path | None = None) -> str:
    """Authenticate the clearsigned payload, then bind it to the separate txt.

    Use a private GnuPG home; never retrieve keys from the network. The operator
    supplies an exported release key, or it must already be in their keyring.
    A successful signature from any other fingerprint is insufficient.
    """
    gpg = shutil.which("gpg") or shutil.which("gpg2")
    if not gpg:
        raise InstallError("GnuPG unavailable: cannot verify Node SHASUMS256.txt.asc; refusing install")
    if not signature.read_bytes().startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
        raise InstallError("Node SHASUMS256.txt.asc must be a GPG clearsigned message")
    home = work / "gnupg"
    home.mkdir(mode=0o700)
    base = [gpg, "--no-options", "--batch", "--homedir", str(home), "--no-auto-key-retrieve"]
    if release_key is None:
        exported = subprocess.run(
            [gpg, "--no-options", "--batch", "--export", NODE_RELEASE_FINGERPRINT],
            capture_output=True, timeout=30,
        )
        if exported.returncode or not exported.stdout:
            raise InstallError("pinned Node release key unavailable; supply --node-release-key (no key retrieval)")
        key_data = exported.stdout
    else:
        key_data = release_key.read_bytes()
    imported = subprocess.run(base + ["--import"], input=key_data, capture_output=True, timeout=30)
    if imported.returncode:
        raise InstallError("GnuPG could not import the Node release key; refusing install")
    verified = work / "verified-SHASUMS256.txt"
    result = subprocess.run(
        base + ["--status-fd", "1", "--output", str(verified), "--decrypt", str(signature)],
        capture_output=True, timeout=30,
    )
    status = result.stdout.decode("utf-8", errors="replace").splitlines()
    signers = [line.split()[2] for line in status if line.startswith("[GNUPG:] VALIDSIG ")]
    bad = any(line.startswith(tuple(f"[GNUPG:] {s} " for s in (
        "BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY"))) for line in status)
    if result.returncode or bad or signers != [NODE_RELEASE_FINGERPRINT] or not verified.is_file():
        raise InstallError("Node GPG signature verification failed or signer is not the pinned fingerprint")
    if verified.read_bytes().replace(b"\r\n", b"\n") != checksums.read_bytes().replace(b"\r\n", b"\n"):
        raise InstallError("Node SHASUMS256.txt differs from the GPG-authenticated clearsigned payload")
    return f"GPG-verified nodejs.org SHASUMS256.txt.asc; signing key {NODE_RELEASE_FINGERPRINT}"


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or "\x00" in value or "\\" in value or path.is_absolute()
            or ".." in path.parts or path == PurePosixPath(".")):
        raise InstallError(f"unsafe managed/archive path: {value!r}")
    return path


def extract_archive(archive: Path, destination: Path, prefix: str) -> None:
    """Extract only one expected top directory, with bounded file links.

    Preserve safe npm/Node file symlinks: copying npm's CLI would break its
    relative imports. Hash both alias and target in the ledger, as V1 supports.
    Hardlinks become copies. No tar owner, special file or unsafe link survives.
    """
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    pending = []
    seen = set()
    with tarfile.open(archive, "r:*") as source:
        for member in source:
            path = _relative_path(member.name)
            if path.parts[0] != prefix:
                raise InstallError(f"archive member is outside expected {prefix}/")
            rel = PurePosixPath(*path.parts[1:])
            if rel == PurePosixPath("."):
                if not member.isdir():
                    raise InstallError("archive top level must be a directory")
                continue
            if rel in seen:
                raise InstallError(f"duplicate archive member: {path}")
            seen.add(rel)
            target = destination / rel
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
            elif member.isfile():
                stream = source.extractfile(member)
                if stream is None:
                    raise InstallError(f"archive file cannot be read: {path}")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
            elif member.issym() or member.islnk():
                link = member.linkname
                if not link or "\x00" in link or "\\" in link or PurePosixPath(link).is_absolute():
                    raise InstallError(f"unsafe archive link: {path}")
                normalized = posixpath.normpath(str(path.parent / link) if member.issym() else link)
                linked = _relative_path(normalized)
                if linked.parts[0] != prefix:
                    raise InstallError(f"archive link escapes its root: {path}")
                pending.append((target, destination / PurePosixPath(*linked.parts[1:]), member.issym()))
            else:
                raise InstallError(f"unsupported archive member type: {path}")
    while pending:
        remaining = []
        for target, linked, symlink in pending:
            if linked.is_file():
                if not linked.resolve().is_relative_to(destination.resolve()):
                    raise InstallError("archive link resolves outside its root")
                if symlink:
                    target.symlink_to(os.path.relpath(linked, target.parent))
                else:
                    shutil.copyfile(linked, target)
                    target.chmod(linked.stat().st_mode & 0o700)
            else:
                remaining.append((target, linked, symlink))
        if len(remaining) == len(pending):
            raise InstallError("archive contains dangling, cyclic or directory links")
        pending = remaining


def _owned_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise InstallError(f"installer directory must be owned, non-shared and not a symlink: {path}")


@contextmanager
def install_lock(root: Path, timeout: float = 60):
    """Stable sibling state and flock inode survive runtime directory swaps."""
    if timeout < 0 or not timeout < float("inf"):
        raise InstallError("lock timeout must be finite and non-negative")
    state = root.parent / f"{root.name}.installer"
    state.mkdir(mode=0o700, exist_ok=True)
    _owned_directory(state)
    fd = os.open(state / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise InstallError("installer lock must be an owned, non-shared regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InstallError("timed out waiting for managed runtime install lock")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield state
    finally:
        os.close(fd)  # Also releases automatically if the process dies.


def publish(staged: Path, live: Path) -> None:
    """Atomic first rename or Linux RENAME_EXCHANGE for an existing directory.

    An ordinary rename cannot replace a non-empty directory. Never use a
    two-rename fallback: that leaves a missing-root/crash window.
    """
    if not live.exists():
        os.rename(staged, live)
        return
    _owned_directory(live)
    libc = ctypes.CDLL(None, use_errno=True)
    exchange = getattr(libc, "renameat2", None)
    if exchange is None:
        raise InstallError("atomic directory exchange unavailable; existing runtime left intact")
    exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(staged), -100, os.fsencode(live), 2) != 0:
        error = ctypes.get_errno()
        raise InstallError(f"atomic directory exchange failed: {os.strerror(error)}; existing runtime left intact")


def _contract_record(pins) -> dict:
    return {"node": pins.node_version, "node_platform": pins.node_platform,
            "node_arch": pins.node_arch, "openclaw": pins.openclaw_version,
            "commit": pins.openclaw_commit, "tarball": pins.openclaw_tarball,
            "integrity": pins.openclaw_integrity}


def install(project_root: Path, *, staging_dir: Path | None = None,
            node_release_key: Path | None = None, lock_timeout: float = 60,
            env: dict[str, str] | None = None, runner=None) -> Path:
    """Rebuild safely on every invocation, authenticating even offline artifacts."""
    env = os.environ if env is None else env
    pins = load_contract(project_root, env)
    if env.get("SAM_CLOUD_READONLY") == "1":
        raise InstallError("cloud read-only profile must not install the OpenClaw engine")
    try:
        config = RuntimeConfig.from_env(project_root, env)
    except RuntimeError as exc:
        raise InstallError(f"runtime configuration cannot be loaded: {exc}") from exc
    if config.cloud_readonly:
        raise InstallError("cloud read-only profile must not install the OpenClaw engine")
    runtime = _read_config(project_root, env).get("runtime", {})
    for key, value in _contract_record(pins).items():
        manifest_key = f"openclaw_{key}" if key in {"commit", "tarball", "integrity"} else key
        if runtime.get(manifest_key) != value:
            raise InstallError(f"runtime profile/config drifts from the manifest contract: {manifest_key}")
    entry = config.openclaw_entry
    if tuple(entry.parts[-3:]) != ENTRY_SUFFIX:
        raise InstallError("configured OpenClaw entry must use the canonical managed path")
    root = entry.parents[2]
    _owned_directory(root.parent)
    if root.is_symlink():
        raise InstallError("managed install root must not be a symlink")
    node_dir = f"node-v{pins.node_version}-{pins.node_platform}-{pins.node_arch}"
    node_name = f"{node_dir}.tar.gz"
    node_binary = f"{node_dir}/bin/node"
    if config.node_bin and Path(config.node_bin).resolve() != (root / node_binary).resolve():
        raise InstallError("SAM_NODE_BIN/config does not match the installer-managed Node path")
    dist = f"https://nodejs.org/dist/v{pins.node_version}"
    urls = {PurePosixPath(urlsplit(pins.openclaw_tarball).path).name: pins.openclaw_tarball,
            node_name: f"{dist}/{node_name}", "SHASUMS256.txt": f"{dist}/SHASUMS256.txt",
            "SHASUMS256.txt.asc": f"{dist}/SHASUMS256.txt.asc"}
    allowed = set(urls.values())
    for url in allowed:
        _official_url(url, allowed)
    with install_lock(root, lock_timeout) as state:
        with tempfile.TemporaryDirectory(prefix="stage-", dir=state, ignore_cleanup_errors=True) as temporary:
            work = Path(temporary)  # mkdtemp is 0700, on the install filesystem.
            artifacts = work / "artifacts"
            artifacts.mkdir(mode=0o700)
            for name, url in urls.items():
                target = artifacts / name
                if staging_dir is None:
                    download(url, target, allowed)
                else:
                    source = staging_dir / name
                    if not source.is_file():
                        raise InstallError(f"offline artifact is missing: {name}")
                    shutil.copyfile(source, target)
                target.chmod(0o600)
            openclaw_tar = artifacts / next(iter(urls))
            sri = base64.b64encode(_hash(openclaw_tar, "sha512")).decode("ascii")
            if sri != pins.tarball_sha512_b64:
                raise InstallError("OpenClaw tarball sha512 SRI mismatch; refusing install")
            checksums = artifacts / "SHASUMS256.txt"
            signature_evidence = verify_node_signature(
                checksums, artifacts / "SHASUMS256.txt.asc", work, node_release_key)
            matches = re.findall(rf"^([0-9a-f]{{64}})  {re.escape(node_name)}$",
                                 checksums.read_text(encoding="ascii"), re.MULTILINE)
            if matches != [NODE_TAR_GZ_SHA256]:
                raise InstallError("Node signed SHASUMS line differs from the reviewed official SHA-256")
            if _hash(artifacts / node_name, "sha256").hex() != matches[0]:
                raise InstallError("Node tarball SHA-256 mismatch; refusing install")
            staged = work / "runtime"
            staged.mkdir(mode=0o700)
            extract_archive(openclaw_tar, staged / "node_modules/openclaw", "package")
            extract_archive(artifacts / node_name, staged / node_dir, node_dir)
            files = []
            for path in sorted(staged.rglob("*")):
                if path.is_file():
                    rel = path.relative_to(staged).as_posix()
                    _relative_path(rel)
                    files.append({"path": rel, "sha256": _hash(path, "sha256").hex()})
            ledger = {
                "schema": 1, "contract": _contract_record(pins), "files": files,
                "openclaw": {
                    "commit": pins.openclaw_commit, "tarball_sha512": sri,
                    "commit_provenance": (
                        f"Reviewer-verified 2026-10-29: https://github.com/openclaw/openclaw "
                        f"annotated tag v{pins.openclaw_version} (object {OPENCLAW_TAG_OBJECT}) "
                        f"-> {pins.openclaw_commit}; "
                        f"https://registry.npmjs.org/-/npm/v1/attestations/openclaw@{pins.openclaw_version} "
                        f"https://slsa.dev/provenance/v1, GitHub Actions github-hosted builder, "
                        f".github/workflows/openclaw-npm-release.yml, build commit {OPENCLAW_BUILD_COMMIT}"
                    ),
                },
                "node": {"binary": node_binary, "sha256": _hash(staged / node_binary, "sha256").hex(),
                         "official_sha256": matches[0], "official_signature": signature_evidence},
            }
            ledger_path = staged / LEDGER_NAME
            with ledger_path.open("x", encoding="utf-8") as output:
                ledger_path.chmod(0o600)
                json.dump(ledger, output, indent=2)
                output.write("\n")
            # The unchanged V1 gate checks everything, including exact Node
            # metadata, before the published root can change. No engine runs.
            staged_config = replace(config, openclaw_entry=staged.joinpath(*ENTRY_SUFFIX), node_bin="")
            preflight_runtime(staged_config, env=env, runner=runner)
            publish(staged, root)
            # The old root (after exchange) is now private staging garbage.
    return root
