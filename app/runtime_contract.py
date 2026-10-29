"""Read-only loading of the CHG-070 approved manifest contract."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from .runtime_config import _simple_yaml

if TYPE_CHECKING:
    from .runtime_config import RuntimeConfig


class RuntimeContractError(RuntimeError):
    """The manifest does not restate the approved runtime contract."""


APPROVED_NODE_VERSION = "24.22.1"
APPROVED_NODE_PLATFORM = "linux"
APPROVED_NODE_ARCH = "x64"
APPROVED_OPENCLAW_VERSION = "2026.10.7"
APPROVED_OPENCLAW_COMMIT = "157d8c2bc3ea2fa183afdc11088e69690bf8e64e"
APPROVED_OPENCLAW_TARBALL = "https://registry.npmjs.org/openclaw/-/openclaw-2026.10.7.tgz"
APPROVED_OPENCLAW_INTEGRITY = "sha512-iiZO+kFFBgQvxm17wn3Sc/MSC/GQu01PubAFVNA2Nn736HOHUbR0ra7a9NeW9wpjq3kDkSJ1RB7dTP+qVJFXig=="

_EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_INTEGRITY = re.compile(r"sha512-([A-Za-z0-9+/]+={0,2})")


@dataclass(frozen=True)
class RuntimeContract:
    """The seven immutable values declared by the manifest."""

    node_version: str
    node_platform: str
    node_arch: str
    openclaw_version: str
    openclaw_commit: str
    openclaw_tarball: str
    openclaw_integrity: str

    @property
    def tarball_sha512_b64(self) -> str:
        value = self.openclaw_integrity
        match = _INTEGRITY.fullmatch(value) if type(value) is str else None
        if match is not None:
            body = match.group(1)
            try:
                digest = base64.b64decode(body, validate=True)
            except binascii.Error:
                pass
            else:
                if len(digest) == 64 and base64.b64encode(digest).decode("ascii") == body:
                    return body
        raise RuntimeContractError(f"runtime.openclaw_integrity is invalid: {value!r}")


def _pinned(runtime: dict, name: str, expected: str, pattern: re.Pattern | None = None) -> str:
    value = runtime.get(name)
    if type(value) is not str or not value:
        raise RuntimeContractError(f"runtime.{name} must be a non-empty built-in str, got {value!r}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise RuntimeContractError(f"runtime.{name} has invalid format: {value!r}")
    if value != expected:
        raise RuntimeContractError(f"runtime.{name} must equal {expected!r}, got {value!r}")
    return value


def _pinned_tarball(runtime: dict) -> str:
    value = runtime.get("openclaw_tarball")
    if type(value) is not str or not value:
        raise RuntimeContractError(f"runtime.openclaw_tarball must be a non-empty built-in str, got {value!r}")
    try:
        parts = urlsplit(value)
        official = parts.scheme == "https" and parts.hostname == "registry.npmjs.org"
    except ValueError as exc:
        raise RuntimeContractError(f"runtime.openclaw_tarball is invalid: {value!r}") from exc
    if not official:
        raise RuntimeContractError(f"runtime.openclaw_tarball must use the official HTTPS host, got {value!r}")
    return _pinned(runtime, "openclaw_tarball", APPROVED_OPENCLAW_TARBALL)


def load_contract(root: Path | str, env: dict[str, str] | None = None) -> RuntimeContract:
    """Read only the selected manifest; environment values cannot override pins."""
    env = os.environ if env is None else env
    manifest_path = Path(env.get("SAM_MANIFEST", Path(root) / "sam-manifest.yaml"))
    try:
        document = _simple_yaml(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeContractError(f"runtime manifest unreadable or malformed: {manifest_path}: {exc}") from exc
    runtime = document.get("runtime")
    if not isinstance(runtime, dict):
        raise RuntimeContractError(f"runtime must be a mapping, got {runtime!r}")
    return RuntimeContract(
        node_version=_pinned(runtime, "node", APPROVED_NODE_VERSION, _EXACT_VERSION),
        node_platform=_pinned(runtime, "node_platform", APPROVED_NODE_PLATFORM),
        node_arch=_pinned(runtime, "node_arch", APPROVED_NODE_ARCH),
        openclaw_version=_pinned(runtime, "openclaw", APPROVED_OPENCLAW_VERSION, _EXACT_VERSION),
        openclaw_commit=_pinned(runtime, "openclaw_commit", APPROVED_OPENCLAW_COMMIT, _COMMIT),
        openclaw_tarball=_pinned_tarball(runtime),
        openclaw_integrity=_pinned(runtime, "openclaw_integrity", APPROVED_OPENCLAW_INTEGRITY, _INTEGRITY),
    )


ENTRY_SUFFIX = ("node_modules", "openclaw", "openclaw.mjs")
LEDGER_NAME = "install-ledger.json"
LEDGER_SCHEMA = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class EngineProof:
    """Read-only preflight outcome. Not an official-chain verification."""

    cloud_readonly: bool = False
    contract: "RuntimeContract | None" = None
    install_root: str | None = None
    node_bin: str | None = None
    node_version: str | None = None
    openclaw_entry: str | None = None
    openclaw_version: str | None = None
    openclaw_commit: str | None = None
    ledger_sha256: str | None = None
    # V1 checks ledger/files/contract consistency. Official Node artifact
    # SHA/signature and tarball->commit provenance re-verification land in V2.
    official_chain: str = "unverified-v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeContractError(f"managed file is unreadable: {path}: {exc}") from exc
    return digest.hexdigest()


def _check_owned_directory(path: Path, label: str) -> None:
    try:
        info = path.stat()
    except OSError as exc:
        raise RuntimeContractError(f"{label} is missing: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeContractError(
            f"{label} must be a directory owned by the current user without group/other write access: {path}"
        )


def _read_ledger(real_root: Path) -> tuple[dict[str, Any], str]:
    path = real_root / LEDGER_NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise RuntimeContractError(f"trusted install ledger is missing: {path}") from exc
    except OSError as exc:
        raise RuntimeContractError(f"trusted install ledger is unreadable: {exc}") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
        ):
            raise RuntimeContractError(
                "install ledger must be a non-shared regular file owned by the current user "
                "without group/other write access"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
    except OSError as exc:
        raise RuntimeContractError(f"trusted install ledger is unreadable: {exc}") from exc
    finally:
        os.close(fd)
    try:
        ledger = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeContractError(f"install ledger is not valid JSON: {exc}") from exc
    if not isinstance(ledger, dict) or ledger.get("schema") != LEDGER_SCHEMA:
        raise RuntimeContractError("install ledger schema is missing or unsupported")
    return ledger, hashlib.sha256(raw).hexdigest()


def _check_ledger_contract(ledger: dict[str, Any], contract: RuntimeContract) -> None:
    recorded = ledger.get("contract")
    if not isinstance(recorded, dict):
        raise RuntimeContractError("install ledger does not record the pinned contract it was built from")
    expected = {
        "node": contract.node_version,
        "node_platform": contract.node_platform,
        "node_arch": contract.node_arch,
        "openclaw": contract.openclaw_version,
        "commit": contract.openclaw_commit,
        "tarball": contract.openclaw_tarball,
        "integrity": contract.openclaw_integrity,
    }
    for key, want in expected.items():
        if recorded.get(key) != want:
            raise RuntimeContractError(
                f"install ledger was built for a different contract ({key} mismatch); refusing to run"
            )


def _managed_file(real_root: Path, rel: Any) -> Path:
    if not isinstance(rel, str) or not rel:
        raise RuntimeContractError("managed install ledger records must use non-empty relative paths")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeContractError(f"managed install ledger path escapes the install root: {rel!r}")
    try:
        resolved = (real_root / rel).resolve()
    except (OSError, ValueError) as exc:
        raise RuntimeContractError(f"managed file path is invalid: {rel!r}: {exc}") from exc
    if not resolved.is_relative_to(real_root):
        raise RuntimeContractError(f"managed file resolves outside the install root: {rel!r}")
    return resolved


def _verify_managed_files(ledger: dict[str, Any], real_root: Path) -> dict[str, Path]:
    records = ledger.get("files")
    if not isinstance(records, list) or not records:
        raise RuntimeContractError("install ledger must list the managed files with sha256 digests")
    verified: dict[str, Path] = {}
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeContractError("install ledger file records must be objects")
        rel = record.get("path")
        digest = record.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RuntimeContractError(f"managed file record lacks a sha256 digest: {rel!r}")
        target = _managed_file(real_root, rel)
        if not target.is_file():
            raise RuntimeContractError(f"managed file is missing: {rel}")
        if _sha256(target) != digest:
            raise RuntimeContractError(f"managed file digest mismatch (tampered or stale install): {rel}")
        verified[str(PurePosixPath(rel))] = target
    required = {"/".join(ENTRY_SUFFIX), "node_modules/openclaw/package.json"}
    missing = sorted(required - verified.keys())
    if missing:
        raise RuntimeContractError(f"install ledger does not cover required managed files: {', '.join(missing)}")
    return verified


def _verify_openclaw_package(package_json: Path, contract: RuntimeContract) -> None:
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeContractError(f"managed OpenClaw package.json is unreadable: {exc}") from exc
    version = package.get("version") if isinstance(package, dict) else None
    if version != contract.openclaw_version:
        raise RuntimeContractError(
            f"managed OpenClaw package version must be exactly {contract.openclaw_version}, got {version!r}"
        )


def _verify_openclaw_provenance(ledger: dict[str, Any], contract: RuntimeContract) -> None:
    section = ledger.get("openclaw")
    if not isinstance(section, dict):
        raise RuntimeContractError("install ledger lacks the OpenClaw provenance section")
    if section.get("commit") != contract.openclaw_commit:
        raise RuntimeContractError("install ledger commit provenance does not match the pinned upstream commit")
    provenance = section.get("commit_provenance")
    if not isinstance(provenance, str) or not provenance.strip():
        raise RuntimeContractError(
            "install ledger lacks commit provenance evidence; refusing to guess the commit from the package version"
        )
    if section.get("tarball_sha512") != contract.tarball_sha512_b64:
        raise RuntimeContractError("install ledger tarball digest does not match the pinned official SRI")


def _node_probe(node_bin: Path, args: list[str], run: Callable[..., Any]) -> str:
    try:
        proc = run([str(node_bin), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeContractError(f"managed Node probe failed to execute: {exc}") from exc
    if getattr(proc, "returncode", None) != 0:
        raise RuntimeContractError(f"managed Node probe exited with status {getattr(proc, 'returncode', '?')}")
    return str(proc.stdout or "").strip()


def _verify_managed_node(
    ledger: dict[str, Any],
    real_root: Path,
    config: "RuntimeConfig",
    contract: RuntimeContract,
    run: Callable[..., Any],
) -> Path:
    section = ledger.get("node")
    if not isinstance(section, dict):
        raise RuntimeContractError("install ledger lacks the managed Node section")
    node_bin = _managed_file(real_root, section.get("binary"))
    recorded = section.get("sha256")
    if not isinstance(recorded, str) or not _SHA256.fullmatch(recorded):
        raise RuntimeContractError("install ledger lacks the managed Node binary digest")
    # Official Node artifact evidence is mandatory but only re-verifiable by
    # the V2 installer; V1 fails closed when the evidence fields are absent.
    official_sha = section.get("official_sha256")
    if not isinstance(official_sha, str) or not _SHA256.fullmatch(official_sha):
        raise RuntimeContractError("install ledger lacks the official Node artifact SHA-256 evidence")
    signature = section.get("official_signature")
    if not isinstance(signature, str) or not signature.strip():
        raise RuntimeContractError("install ledger lacks the official Node signature evidence")
    if not node_bin.is_file() or not os.access(node_bin, os.X_OK):
        raise RuntimeContractError(f"managed Node executable is missing or not executable: {node_bin}")
    # Integrity before execution: never run a managed file that drifted.
    if _sha256(node_bin) != recorded:
        raise RuntimeContractError(
            "managed Node binary digest mismatch (tampered or stale install); refusing to execute it"
        )
    configured = (config.node_bin or "").strip()
    try:
        configured_path = Path(configured).resolve() if configured else None
    except (OSError, ValueError) as exc:
        raise RuntimeContractError(f"SAM_NODE_BIN path is invalid: {configured!r}: {exc}") from exc
    if configured_path is not None and configured_path != node_bin:
        raise RuntimeContractError(
            "SAM_NODE_BIN does not resolve to the trusted install ledger's managed Node; "
            "environment and symlink overrides cannot redirect the engine"
        )
    version_out = _node_probe(node_bin, ["--version"], run)
    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+)", version_out)
    if not match or match.group(1) != contract.node_version:
        raise RuntimeContractError(
            f"managed Node must report exactly v{contract.node_version}, got {version_out!r}"
        )
    platform_out = _node_probe(node_bin, ["-p", "process.platform + '/' + process.arch"], run)
    expected = f"{contract.node_platform}/{contract.node_arch}"
    if platform_out != expected:
        raise RuntimeContractError(f"managed Node must run on {expected}, got {platform_out!r}")
    return node_bin


def preflight_runtime(
    config: "RuntimeConfig",
    *,
    env: dict[str, str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> EngineProof:
    """Read-only fail-closed gate for every OpenClaw engine entry point.

    Returns an :class:`EngineProof` on success and raises
    :class:`RuntimeContractError` otherwise. It performs no writes, reclaims
    no ports and starts no engine process; the only execution is the trusted
    managed Node binary reporting its own version/platform after its digest
    has been verified against the install ledger.
    """
    env = os.environ if env is None else env
    run = subprocess.run if runner is None else runner
    contract = load_contract(config.project_root, env)
    if config.cloud_readonly:
        # Cloud snapshot windows are read-only and must never start an engine.
        return EngineProof(cloud_readonly=True, contract=contract)

    entry = Path(config.openclaw_entry)
    if len(entry.parts) < 3 or tuple(entry.parts[-3:]) != ENTRY_SUFFIX:
        raise RuntimeContractError(
            f"OpenClaw entry must be the canonical {'/'.join(ENTRY_SUFFIX)} inside a managed install root: {entry}"
        )
    managed_root = entry.parents[2]
    try:
        real_root = managed_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeContractError(f"managed OpenClaw install root is missing: {managed_root}") from exc
    _check_owned_directory(real_root, "managed OpenClaw install root")
    try:
        real_entry = entry.resolve(strict=True)
    except OSError as exc:
        raise RuntimeContractError(f"OpenClaw entry is missing: {entry}") from exc
    if not real_entry.is_relative_to(real_root):
        raise RuntimeContractError(f"OpenClaw entry resolves outside the managed install root: {entry}")
    if tuple(real_entry.relative_to(real_root).parts) != ENTRY_SUFFIX:
        raise RuntimeContractError(
            f"OpenClaw entry must stay the canonical {'/'.join(ENTRY_SUFFIX)} after resolving links: {entry}"
        )

    ledger, ledger_digest = _read_ledger(real_root)
    _check_ledger_contract(ledger, contract)
    files = _verify_managed_files(ledger, real_root)
    _verify_openclaw_package(files["node_modules/openclaw/package.json"], contract)
    _verify_openclaw_provenance(ledger, contract)
    node_bin = _verify_managed_node(ledger, real_root, config, contract, run)
    return EngineProof(
        cloud_readonly=False,
        contract=contract,
        install_root=str(real_root),
        node_bin=str(node_bin),
        node_version=contract.node_version,
        openclaw_entry=str(real_entry),
        openclaw_version=contract.openclaw_version,
        openclaw_commit=contract.openclaw_commit,
        ledger_sha256=ledger_digest,
    )
