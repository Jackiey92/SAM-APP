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


# V1B: synthetic managed installs; no real Node or runtime configuration probes.
@pytest.fixture
def managed_install(tmp_path):
    import hashlib
    from types import SimpleNamespace

    write_manifest(tmp_path)
    root = tmp_path / "managed"
    entry = root / "node_modules/openclaw/openclaw.mjs"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"synthetic entry\n")
    package = entry.with_name("package.json")
    package.write_text(json.dumps({"version": PINS["openclaw"]}), encoding="utf-8")
    node = root / "bin/node"
    node.parent.mkdir()
    node.write_bytes(b"synthetic node; never execute")
    node.chmod(0o700)
    ledger = {
        "schema": 1,
        "contract": {"node": PINS["node"], "node_platform": PINS["node_platform"],
                     "node_arch": PINS["node_arch"], "openclaw": PINS["openclaw"],
                     "commit": PINS["openclaw_commit"], "tarball": PINS["openclaw_tarball"],
                     "integrity": PINS["openclaw_integrity"]},
        "files": [{"path": str(p.relative_to(root)),
                   "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (entry, package)],
        "openclaw": {"commit": PINS["openclaw_commit"], "commit_provenance": "synthetic evidence",
                     "tarball_sha512": PINS["openclaw_integrity"][7:]},
        "node": {"binary": "bin/node", "sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
                 "official_sha256": "a" * 64, "official_signature": "synthetic signature evidence"},
    }
    ledger_path = root / contract.LEDGER_NAME
    calls = []

    def save():
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        ledger_path.chmod(0o600)

    def runner(argv, **kwargs):
        assert argv[0] == str(node)
        assert kwargs == {"capture_output": True, "text": True, "timeout": 10}
        assert argv[1:] in (["--version"], ["-p", "process.platform + '/' + process.arch"])
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="v24.22.1\n" if argv[1] == "--version" else "linux/x64\n")

    config = SimpleNamespace(project_root=tmp_path, openclaw_entry=entry, node_bin="", cloud_readonly=False)
    save()
    return SimpleNamespace(root=root, entry=entry, package=package, node=node, ledger=ledger,
                           ledger_path=ledger_path, save=save, runner=runner, calls=calls, config=config)


def run_preflight(install, runner=None):
    return contract.preflight_runtime(install.config, env={}, runner=install.runner if runner is None else runner)


def test_preflight_proof_and_frozen(managed_install):
    import hashlib

    i = managed_install
    i.config.node_bin = str(i.node)
    proof = run_preflight(i)
    assert asdict(proof) == {
        "cloud_readonly": False, "contract": EXPECTED, "install_root": str(i.root),
        "node_bin": str(i.node), "node_version": PINS["node"], "openclaw_entry": str(i.entry),
        "openclaw_version": PINS["openclaw"], "openclaw_commit": PINS["openclaw_commit"],
        "ledger_sha256": hashlib.sha256(i.ledger_path.read_bytes()).hexdigest(),
        "official_chain": "unverified-v2",
    }
    assert len(i.calls) == 2
    with pytest.raises(FrozenInstanceError):
        proof.node_bin = "elsewhere"


@pytest.mark.parametrize("valid", [True, False])
def test_preflight_cloud_only_reads_manifest(tmp_path, monkeypatch, valid):
    from types import SimpleNamespace

    manifest = write_manifest(tmp_path, PINS if valid else {**PINS, "node": "latest"})
    config = SimpleNamespace(project_root=tmp_path, cloud_readonly=True)
    read_text = Path.read_text
    reads = []

    def read_manifest(path, *args, **kwargs):
        assert path == manifest
        reads.append(path)
        return read_text(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("cloud preflight must only load the manifest")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_manifest)
        for name in ("exists", "is_file", "is_dir", "resolve", "stat", "mkdir", "write_text", "write_bytes"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(contract.os, "open", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(socket, "socket", forbidden)
        if valid:
            proof = contract.preflight_runtime(config, env={}, runner=forbidden)
            assert proof.cloud_readonly is True
            assert_contract(proof.contract)
            assert proof == contract.EngineProof(cloud_readonly=True, contract=proof.contract)
        else:
            with pytest.raises(contract.RuntimeContractError, match="runtime.node"):
                contract.preflight_runtime(config, env={}, runner=forbidden)
    assert reads == [manifest]


@pytest.mark.parametrize(("case", "message"), [
    ("ledger_missing", "ledger is missing"), ("ledger_symlink", "ledger is unreadable"),
    ("ledger_mode", "non-shared regular file"), ("ledger_hardlink", "non-shared regular file"),
    ("ledger_directory", "non-shared regular file"),
    ("ledger_json", "not valid JSON"), ("ledger_utf8", "not valid JSON"),
    ("ledger_schema", "schema"), ("ledger_scalar", "schema"),
    ("contract_section", "does not record"),
    ("absolute_path", "escapes"), ("parent_path", "escapes"), ("outside_path", "outside"),
    ("empty_path", "non-empty relative"), ("nonstring_path", "non-empty relative"),
    ("files_empty", "must list"), ("files_mapping", "must list"), ("file_record", "must be objects"),
    ("digest_missing", "lacks a sha256"), ("digest_invalid", "lacks a sha256"),
    ("file_missing", "managed file is missing"), ("file_drift", "file digest mismatch"),
    ("missing_entry_record", "does not cover"), ("missing_package_record", "does not cover"),
    ("package_version", "package version"), ("package_json", "package.json is unreadable"),
    ("package_scalar", "package version"),
    ("provenance_section", "provenance section"), ("commit_missing", "commit provenance"),
    ("provenance_empty", "lacks commit provenance"), ("tarball_wrong", "tarball digest"),
    ("node_section", "managed Node section"), ("node_digest_missing", "binary digest"),
    ("official_sha_missing", "SHA-256 evidence"), ("official_sha_invalid", "SHA-256 evidence"),
    ("official_signature_missing", "signature evidence"), ("official_signature_empty", "signature evidence"),
    ("node_missing", "missing or not executable"), ("node_mode", "missing or not executable"),
    ("node_drift", "Node binary digest mismatch"), ("node_redirect", "SAM_NODE_BIN"),
    ("entry_shape", "canonical"), ("entry_missing", "entry is missing"),
    ("entry_outside", "entry resolves outside"), ("entry_alias", "after resolving links"),
    ("root_missing", "install root is missing"), ("root_mode", "directory owned"),
])
def test_preflight_rejects_evidence(managed_install, case, message):
    import hashlib
    import os

    i = managed_install
    ledger = i.ledger
    if case == "ledger_missing":
        i.ledger_path.unlink()
    elif case == "ledger_symlink":
        target = i.root / "ledger-target"
        i.ledger_path.rename(target)
        i.ledger_path.symlink_to(target)
    elif case == "ledger_mode":
        i.ledger_path.chmod(0o620)
    elif case == "ledger_hardlink":
        os.link(i.ledger_path, i.root / "ledger-link")
    elif case == "ledger_directory":
        i.ledger_path.unlink()
        i.ledger_path.mkdir()
    elif case in ("ledger_json", "ledger_utf8", "ledger_scalar"):
        i.ledger_path.write_bytes({"ledger_json": b"{", "ledger_utf8": b"\xff", "ledger_scalar": b"[]"}[case])
    elif case == "ledger_schema":
        ledger["schema"] = 2
    elif case == "contract_section":
        ledger["contract"] = []
    elif case in ("absolute_path", "parent_path", "outside_path", "empty_path", "nonstring_path"):
        outside = i.root.parent / "outside"
        outside.write_bytes(b"outside")
        (i.root / "escape").symlink_to(outside)
        ledger["files"][0]["path"] = {"absolute_path": str(outside), "parent_path": "../outside",
                                       "outside_path": "escape", "empty_path": "", "nonstring_path": None}[case]
    elif case in ("files_empty", "files_mapping", "file_record"):
        ledger["files"] = {"files_empty": [], "files_mapping": {}, "file_record": [None]}[case]
    elif case == "digest_missing":
        del ledger["files"][0]["sha256"]
    elif case == "digest_invalid":
        ledger["files"][0]["sha256"] = "xyz"
    elif case == "file_missing":
        i.package.unlink()
    elif case == "file_drift":
        i.package.write_bytes(b"changed")
    elif case == "missing_entry_record":
        ledger["files"].pop(0)
    elif case == "missing_package_record":
        ledger["files"].pop(1)
    elif case in ("package_version", "package_json", "package_scalar"):
        i.package.write_text({"package_version": '{"version":"2026.10.8"}',
                              "package_json": "{", "package_scalar": "[]"}[case], encoding="utf-8")
        ledger["files"][1]["sha256"] = hashlib.sha256(i.package.read_bytes()).hexdigest()
    elif case == "provenance_section":
        del ledger["openclaw"]
    elif case == "commit_missing":
        del ledger["openclaw"]["commit"]
    elif case == "provenance_empty":
        ledger["openclaw"]["commit_provenance"] = " \n"
    elif case == "tarball_wrong":
        ledger["openclaw"]["tarball_sha512"] = "wrong"
    elif case == "node_section":
        del ledger["node"]
    elif case in ("node_digest_missing", "official_sha_missing", "official_signature_missing"):
        del ledger["node"][{"node_digest_missing": "sha256", "official_sha_missing": "official_sha256",
                             "official_signature_missing": "official_signature"}[case]]
    elif case == "official_sha_invalid":
        ledger["node"]["official_sha256"] = "bad"
    elif case == "official_signature_empty":
        ledger["node"]["official_signature"] = " \n"
    elif case == "node_missing":
        i.node.unlink()
    elif case == "node_mode":
        i.node.chmod(0o600)
    elif case == "node_drift":
        i.node.write_bytes(b"changed node")
    elif case == "node_redirect":
        i.config.node_bin = str(i.package)
    elif case == "entry_shape":
        i.config.openclaw_entry = i.root / "openclaw.mjs"
    elif case == "entry_missing":
        i.entry.unlink()
    elif case in ("entry_outside", "entry_alias"):
        target = (i.root.parent if case == "entry_outside" else i.root) / "alias.mjs"
        target.write_bytes(i.entry.read_bytes())
        i.entry.unlink()
        i.entry.symlink_to(target)
    elif case == "root_missing":
        i.config.openclaw_entry = i.root / "absent/node_modules/openclaw/openclaw.mjs"
    elif case == "root_mode":
        i.root.chmod(0o702)
    else:
        pytest.fail(f"unhandled case {case}")
    if not case.startswith("ledger_") or case == "ledger_schema":
        i.save()
    with pytest.raises(contract.RuntimeContractError, match=message):
        run_preflight(i)
    assert i.calls == []


@pytest.mark.parametrize("key", ["node", "node_platform", "node_arch", "openclaw", "commit", "tarball", "integrity"])
def test_preflight_ledger_contract_mismatch(managed_install, key):
    i = managed_install
    i.ledger["contract"][key] = "wrong"
    i.save()
    with pytest.raises(contract.RuntimeContractError, match=f"{key} mismatch"):
        run_preflight(i)
    assert i.calls == []


@pytest.mark.parametrize("probe", ["version", "platform"])
@pytest.mark.parametrize("failure", ["output", "oserror", "timeout", "nonzero"])
def test_preflight_node_probe_failures(managed_install, probe, failure):
    from types import SimpleNamespace

    i = managed_install
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if (argv[1] == "--version") == (probe == "version"):
            if failure == "oserror":
                raise OSError("synthetic failure")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 10)
            return SimpleNamespace(returncode=1 if failure == "nonzero" else 0, stdout="wrong")
        return i.runner(argv, **kwargs)

    with pytest.raises(contract.RuntimeContractError, match="probe|must report exactly|must run on"):
        run_preflight(i, runner)
    assert len(calls) == (1 if probe == "version" else 2)


@pytest.mark.parametrize("target", ["root", "ledger"])
def test_preflight_wrong_owner(managed_install, monkeypatch, target):
    # Simulate owner mismatch without requiring chown privileges.
    monkeypatch.setattr(contract.os, "getuid", lambda: managed_install.root.stat().st_uid + 1)
    if target == "root":
        with pytest.raises(contract.RuntimeContractError, match="directory owned"):
            run_preflight(managed_install)
    else:
        with pytest.raises(contract.RuntimeContractError, match="non-shared regular file"):
            contract._read_ledger(managed_install.root)
    assert managed_install.calls == []


def test_preflight_success_is_read_only(managed_install, monkeypatch):
    import builtins
    import io
    import os

    i = managed_install
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in i.root.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("preflight attempted a write, real subprocess or network access")

    def readonly_open(original):
        def checked(file, mode="r", *args, **kwargs):
            assert not any(flag in mode for flag in "wax+")
            return original(file, mode, *args, **kwargs)
        return checked

    original_os_open = os.open

    def checked_os_open(path, flags, *args, **kwargs):
        assert flags == os.O_RDONLY | os.O_NOFOLLOW
        return original_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        for name in ("mkdir", "write_text", "write_bytes", "touch", "unlink", "rename", "replace", "chmod"):
            patch.setattr(Path, name, forbidden)
        for name in ("mkdir", "makedirs", "write", "unlink", "remove", "rename", "replace", "chmod", "system"):
            patch.setattr(os, name, forbidden)
        patch.setattr(builtins, "open", readonly_open(builtins.open))
        patch.setattr(io, "open", readonly_open(io.open))
        patch.setattr(os, "open", checked_os_open)
        patch.setattr(subprocess, "run", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        assert run_preflight(i).node_bin == str(i.node)
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    assert len(i.calls) == 2


@pytest.mark.parametrize(("section", "key", "bad", "message"), [
    ("node", "binary", None, "non-empty relative"),
    ("node", "binary", "/bin/node", "escapes"),
    ("node", "binary", "../node", "escapes"),
    ("node", "sha256", "bad", "binary digest"),
    ("node", "sha256", None, "binary digest"),
    ("node", "official_sha256", None, "SHA-256 evidence"),
    ("node", "official_signature", None, "signature evidence"),
    ("openclaw", "commit", "0" * 40, "commit provenance"),
    ("openclaw", "commit_provenance", None, "lacks commit provenance"),
])
def test_preflight_invalid_node_and_provenance_fields(managed_install, section, key, bad, message):
    i = managed_install
    i.ledger[section][key] = bad
    i.save()
    with pytest.raises(contract.RuntimeContractError, match=message):
        run_preflight(i)
    assert i.calls == []


@pytest.mark.parametrize("redirect", [False, True])
def test_preflight_node_config_symlink(managed_install, redirect):
    i = managed_install
    link = i.root / "node-link"
    link.symlink_to(i.package if redirect else i.node)
    i.config.node_bin = str(link)
    if redirect:
        with pytest.raises(contract.RuntimeContractError, match="SAM_NODE_BIN"):
            run_preflight(i)
        assert i.calls == []
    else:
        assert run_preflight(i).node_bin == str(i.node)


@pytest.mark.parametrize("kind", ["missing", "file", "group_write", "other_write"])
def test_owned_directory_rejects_invalid(tmp_path, kind):
    path = tmp_path / "root"
    if kind == "file":
        path.write_bytes(b"file")
    elif kind != "missing":
        path.mkdir()
        path.chmod(0o720 if kind == "group_write" else 0o702)
    with pytest.raises(contract.RuntimeContractError, match="missing|directory owned"):
        contract._check_owned_directory(path, "test root")


def test_preflight_unreadable_managed_file(managed_install, monkeypatch):
    i = managed_install
    original = Path.open

    def fail_package(path, *args, **kwargs):
        if path == i.package:
            raise PermissionError("synthetic read denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_package)
    with pytest.raises(contract.RuntimeContractError, match="managed file is unreadable"):
        run_preflight(i)
    assert i.calls == []


@pytest.mark.parametrize("field", ["files", "node", "config"])
def test_preflight_invalid_path_value(managed_install, field):
    i = managed_install
    if field == "files":
        i.ledger["files"][0]["path"] = "invalid\x00path"
    elif field == "node":
        i.ledger["node"]["binary"] = "invalid\x00path"
    else:
        i.config.node_bin = "invalid\x00path"
    i.save()
    with pytest.raises(contract.RuntimeContractError, match="path is invalid"):
        run_preflight(i)
    assert i.calls == []


@pytest.mark.parametrize("use_default_runner", [False, True])
def test_preflight_manifest_env_and_runner_selection(managed_install, monkeypatch, use_default_runner):
    i = managed_install
    selected = write_manifest(i.config.project_root, name="selected.yaml")
    write_manifest(i.config.project_root, {**PINS, "node": "latest"})
    monkeypatch.setenv("SAM_MANIFEST", str(selected))
    monkeypatch.setattr(subprocess, "run", i.runner)
    kwargs = {} if use_default_runner else {"runner": i.runner}
    assert contract.preflight_runtime(i.config, **kwargs).node_bin == str(i.node)
    assert len(i.calls) == 2


def test_sha256_multiple_chunks(tmp_path):
    import hashlib

    data = b"a" * ((1 << 20) + 17)
    path = tmp_path / "large"
    path.write_bytes(data)
    assert contract._sha256(path) == hashlib.sha256(data).hexdigest()
