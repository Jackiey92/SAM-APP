"""CHG-070 V1A contract matrix, using only synthetic manifests."""
from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
import socket
import subprocess

import pytest

from app import runtime_contract as contract


ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "node": "24.22.1",
    "node_platform": "linux",
    "node_arch": "x64",
    "openclaw": "2026.10.7",
    "openclaw_commit": "157d8c2bc3ea2fa183afdc11088e69690bf8e64e",
    "openclaw_tarball": "https://registry.npmjs.org/openclaw/-/openclaw-2026.10.7.tgz",
    "openclaw_integrity": "sha512-iiZO+kFFBgQvxm17wn3Sc/MSC/GQu01PubAFVNA2Nn736HOHUbR0ra7a9NeW9wpjq3kDkSJ1RB7dTP+qVJFXig==",
}
EXPECTED = {
    ("node_version" if key == "node" else "openclaw_version" if key == "openclaw" else key): value
    for key, value in PINS.items()
}


def write_manifest(tmp_path, values=None, name="sam-manifest.yaml"):
    path = tmp_path / name
    values = PINS if values is None else values
    path.write_text("runtime:\n" + "".join(
        f"  {key}: {json.dumps(value)}\n" for key, value in values.items()
    ), encoding="utf-8")
    return path


def assert_contract(result):
    assert asdict(result) == EXPECTED
    assert result.tarball_sha512_b64 == PINS["openclaw_integrity"][7:]


def test_repository_manifest():
    assert_contract(contract.load_contract(ROOT, env={}))


def test_synthetic_manifest_and_frozen_result(tmp_path):
    write_manifest(tmp_path)
    result = contract.load_contract(tmp_path, env={})
    assert_contract(result)
    with pytest.raises(FrozenInstanceError):
        result.node_version = "24.16.0"
    with pytest.raises(FrozenInstanceError):
        result.tarball_sha512_b64 = "other"


@pytest.mark.parametrize("default_env", [False, True])
def test_manifest_selection(tmp_path, monkeypatch, default_env):
    selected = write_manifest(tmp_path, name="selected.yaml")
    write_manifest(tmp_path, {**PINS, "node": "latest"})
    env = {"SAM_MANIFEST": str(selected)}
    monkeypatch.setenv("SAM_MANIFEST", str(selected))
    assert_contract(contract.load_contract(tmp_path, None if default_env else env))


@pytest.mark.parametrize("key", PINS)
@pytest.mark.parametrize("bad", [None, "", False, True, 24, 24.22, [], {}])
def test_missing_empty_or_non_string(tmp_path, key, bad):
    values = {**PINS, key: bad}
    write_manifest(tmp_path, values)
    with pytest.raises(contract.RuntimeContractError) as error:
        contract.load_contract(tmp_path, env={})
    assert f"runtime.{key}" in str(error.value)
    assert repr(bad) in str(error.value)


@pytest.mark.parametrize("key", PINS)
def test_missing_key(tmp_path, key):
    write_manifest(tmp_path, {name: value for name, value in PINS.items() if name != key})
    with pytest.raises(contract.RuntimeContractError, match=rf"runtime\.{key}.*None"):
        contract.load_contract(tmp_path, env={})


@pytest.mark.parametrize("key", ["node", "openclaw"])
@pytest.mark.parametrize("bad", [
    "24.16.x", "^24.22.1", "~24.22.1", "24", "latest", "2026.9.6", "2026.10.8",
    "24.22.2", "24.22.1 ", "v24.22.1", ">=24.22.1", "next",
])
def test_version_rejected(tmp_path, key, bad):
    write_manifest(tmp_path, {**PINS, key: bad})
    with pytest.raises(contract.RuntimeContractError) as error:
        contract.load_contract(tmp_path, env={})
    assert f"runtime.{key}" in str(error.value)
    assert repr(bad) in str(error.value)


@pytest.mark.parametrize(("key", "bad"), [
    ("node_platform", "darwin"),
    ("node_arch", "arm64"),
    ("openclaw_commit", PINS["openclaw_commit"][:-1]),
    ("openclaw_commit", PINS["openclaw_commit"].upper()),
    ("openclaw_commit", "v" + PINS["openclaw_commit"]),
    ("openclaw_commit", "0" * 40),
    ("openclaw_integrity", "sha256:" + "a" * 64),
    ("openclaw_integrity", PINS["openclaw_integrity"][7:]),
    ("openclaw_integrity", "sha512-@@=="),
    ("openclaw_integrity", "sha512-a"),
    ("openclaw_integrity", "sha512-" + "A" * 86 + "=="),
    ("openclaw_tarball", PINS["openclaw_tarball"].replace("https:", "http:")),
    ("openclaw_tarball", "https://npmjs.org.evil.com/openclaw/-/openclaw-2026.10.7.tgz"),
    ("openclaw_tarball", PINS["openclaw_tarball"].replace("registry.npmjs.org", "registry.npmjs.org.evil.com")),
    ("openclaw_tarball", PINS["openclaw_tarball"].replace("2026.10.7.tgz", "2026.10.8.tgz")),
    ("openclaw_tarball", PINS["openclaw_tarball"] + "?other=1"),
    ("openclaw_tarball", PINS["openclaw_tarball"] + "#fragment"),
    ("openclaw_tarball", PINS["openclaw_tarball"].replace("https://", "https://user@")),
    ("openclaw_tarball", "https://[broken/openclaw.tgz"),
])
def test_invalid_or_unapproved_pin(tmp_path, key, bad):
    write_manifest(tmp_path, {**PINS, key: bad})
    with pytest.raises(contract.RuntimeContractError) as error:
        contract.load_contract(tmp_path, env={})
    assert f"runtime.{key}" in str(error.value)
    assert repr(bad) in str(error.value)


@pytest.mark.parametrize("text", [
    "broken yaml", "runtime:\n  broken yaml", "other: {}\n", "runtime: []\n",
    "runtime:\n  - node: 24.22.1\n", "runtime: true\n", "runtime: null\n", "",
])
def test_malformed_or_missing_runtime(tmp_path, text):
    (tmp_path / "sam-manifest.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(contract.RuntimeContractError):
        contract.load_contract(tmp_path, env={})


def test_unreadable_manifest(tmp_path):
    with pytest.raises(contract.RuntimeContractError):
        contract.load_contract(tmp_path, env={})


def test_invalid_utf8(tmp_path):
    (tmp_path / "sam-manifest.yaml").write_bytes(b"\xff")
    with pytest.raises(contract.RuntimeContractError):
        contract.load_contract(tmp_path, env={})


@pytest.mark.parametrize("bad", [None, False, "sha512-a", "sha512-@@==", "sha512-AAAA", "sha256:abc"])
def test_integrity_property_rejects_invalid_format(tmp_path, bad):
    write_manifest(tmp_path)
    result = contract.load_contract(tmp_path, env={})
    with pytest.raises(contract.RuntimeContractError, match="openclaw_integrity"):
        _ = replace(result, openclaw_integrity=bad).tarball_sha512_b64


def test_environment_and_profiles_cannot_relax_pins(tmp_path):
    selected = write_manifest(tmp_path, {**PINS, "node": "latest"})
    env = {"SAM_MANIFEST": str(selected), "SAM_NODE_VERSION": PINS["node"],
           "SAM_OPENCLAW_VERSION": PINS["openclaw"], "SAM_PROFILE_PATH": "/absent/profile.yaml"}
    with pytest.raises(contract.RuntimeContractError, match="runtime.node"):
        contract.load_contract(tmp_path, env=env)
    write_manifest(tmp_path)
    env.update(SAM_NODE_VERSION="latest", SAM_OPENCLAW_VERSION="latest")
    assert_contract(contract.load_contract(tmp_path, env=env))


@pytest.mark.parametrize("valid", [True, False])
def test_read_only_without_probes(tmp_path, monkeypatch, valid):
    manifest = write_manifest(tmp_path, PINS if valid else {**PINS, "node": "latest"})
    (tmp_path / "sentinel").write_bytes(b"unchanged")

    def snapshot():
        return {str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in tmp_path.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("contract must not probe paths, start subprocesses or access the network")

    read_text = Path.read_text
    reads = []

    def read_manifest(path, *args, **kwargs):
        assert path == manifest
        reads.append(path)
        return read_text(path, *args, **kwargs)

    before = snapshot()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_manifest)
        for name in ("exists", "is_file", "is_dir", "resolve", "stat", "mkdir", "write_text", "write_bytes", "unlink"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(socket, "socket", forbidden)
        if valid:
            assert_contract(contract.load_contract(tmp_path, env={}))
        else:
            with pytest.raises(contract.RuntimeContractError):
                contract.load_contract(tmp_path, env={})
    assert reads == [manifest]
    assert snapshot() == before
