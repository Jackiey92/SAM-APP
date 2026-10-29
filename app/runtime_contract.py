"""Read-only loading of the CHG-070 approved manifest contract."""
from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .runtime_config import _simple_yaml


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
