from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import socket
import sys
from pathlib import Path

import pytest

from tests.runtime_install import managed_config, managed_runner


ROOT = Path(__file__).resolve().parents[1]


def test_prepare_runtime_mounts_plugin_and_writes_fresh_9_6_shape(tmp_path: Path, monkeypatch, capsys) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    assert port != 18790
    state = tmp_path / "state"
    env = os.environ.copy()
    env.update(
        {
            "SAM_PROFILE": "local",
            "SAM_OPENCLAW_GATEWAY_PORT": str(port),
            "SAM_OPENCLAW_STATE_DIR": str(state),
            "SAM_OPENCLAW_CONFIG": str(state / "openclaw.json"),
        }
    )
    # Exercise real preparation in-process with a synthetic managed installation.
    from app.runtime_config import RuntimeConfig
    config = managed_config(RuntimeConfig.from_env(project_root=ROOT, env=env), tmp_path)
    spec = importlib.util.spec_from_file_location("prepare_bootstrap", ROOT / "scripts/prepare-openclaw-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.RuntimeConfig, "from_env", classmethod(lambda cls, **kwargs: config))
    monkeypatch.setattr(subprocess, "run", managed_runner())
    assert module.main([]) == 0
    assert "openclaw_runtime_prepared" in capsys.readouterr().out
    assert (state / "extensions" / "sam-memory").is_symlink()
    config = json.loads((state / "openclaw.json").read_text(encoding="utf-8"))
    assert "sam-leader" in config["agents"]["entries"]
    assert config["agents"]["entries"]["sam-leader"]["name"] == "SAM Leader"
    assert "list" not in config["agents"]
    assert set(config["agents"]["entries"]["sam-leader"]["tools"]["allow"]) == {
        "sam_memory_read",
        "sam_memory_search",
        "sam_file_get",
        "sam_memory_map",
        "sam_conversation_read",
        "sam_thread_list",
        "sam_thread_open",
        "sam_document_ingest",
    }
    assert "sam_promote" not in config["tools"]["allow"]
    assert config["tools"]["toolSearch"] == {
        "enabled": True,
        "mode": "tools",
        "searchDefaultLimit": 5,
        "maxSearchLimit": 10,
    }
    assert config["tools"]["codeMode"]["enabled"] is False
    assert config["agents"]["defaults"]["models"]["token-plan/${SAM_LEADER_MODEL}"]["params"]["enable_thinking"] is False


def test_default_token_plan_model_is_qwen38max_without_model_override() -> None:
    manifest = (ROOT / "sam-manifest.yaml").read_text(encoding="utf-8")
    startup = (ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
    isolated = (ROOT / "tools" / "run-sam-isolated.sh").read_text(encoding="utf-8")
    assert "default_model: qwen3.8-max" in manifest
    assert 'SAM_LEADER_MODEL:-${SAM_GUIDE_MODEL:-qwen3.8-max}' in startup
    assert 'SAM_LEADER_MODEL:-${SAM_GUIDE_MODEL:-qwen3.8-max}' in isolated


def test_legacy_guide_model_environment_falls_back_to_leader_names() -> None:
    from app.runtime_config import RuntimeConfig

    env = {
        "SAM_MEMORY_ROOT_URI": "sam://sam-test/product-root",
        "SAM_PROFILE": "local",
        "SAM_GUIDE_MODEL": "legacy-model",
        "SAM_GUIDE_MODEL_BASE_URL": "https://legacy.invalid/v1",
        "SAM_GUIDE_MODEL_API_KEY": "legacy-secret",
    }
    config = RuntimeConfig.from_env(project_root=ROOT, env=env)
    assert config.model_default == "legacy-model"
    assert config.model_base_url == "https://legacy.invalid/v1"
    assert config.model_name_env == "SAM_LEADER_MODEL"
    assert config.model_base_url_env == "SAM_LEADER_MODEL_BASE_URL"
    assert config.model_api_key_env == "SAM_LEADER_MODEL_API_KEY"
    assert env["SAM_LEADER_MODEL"] == "legacy-model"
    assert env["SAM_LEADER_MODEL_API_KEY"] == "legacy-secret"


def test_standard_startup_fails_before_flask_when_model_key_is_missing(tmp_path: Path) -> None:
    startup = (ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
    assert "export PORT=18789" in startup
    assert 'export OPENCLAW_GATEWAY_PORT="${SAM_OPENCLAW_GATEWAY_PORT:-${OPENCLAW_GATEWAY_PORT:-18790}}"' in startup
    assert "export SAM_OPENCLAW_GATEWAY_PORT=\"$OPENCLAW_GATEWAY_PORT\"" in startup
    env = os.environ.copy()
    env.pop("SAM_LEADER_MODEL_API_KEY", None)
    env["HOME"] = str(tmp_path)
    env["SAM_NODE_BIN"] = str(_fake_node(tmp_path / "node24", "v24.21.0"))
    result = subprocess.run(
        ["bash", "scripts/run.sh"], cwd=ROOT, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "Missing SAM_LEADER_MODEL_API_KEY" in result.stderr


def test_startup_without_legacy_root_reaches_prepare(tmp_path: Path) -> None:
    fake = tmp_path / "python3"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    env = {"PATH": str(tmp_path) + ":/usr/bin:/bin", "HOME": str(tmp_path),
           "SAM_LEADER_MODEL_API_KEY": "synthetic", "CALLS": str(calls),
           "SAM_DATA_ROOT": str(tmp_path / "data"),
           "SAM_NODE_BIN": str(_fake_node(tmp_path / "node", "v24.21.0"))}
    result = subprocess.run(["bash", "scripts/run.sh"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert lines[0].startswith('-c from app.db.database import validate_schema;')
    assert lines[1:] == ["scripts/prepare-openclaw-runtime.py", "webapp/app.py"]


def test_runtime_config_uses_logical_root_without_injection() -> None:
    from app.runtime_config import RuntimeConfig
    config = RuntimeConfig.from_env(project_root=ROOT, env={"SAM_PROFILE": "local"})
    assert config.memory_root_uri == "sam://facts"
    assert config.memory_provider == "localvector"


def _fake_node(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_runtime_does_not_select_local_or_path_node(tmp_path: Path, monkeypatch) -> None:
    from app.runtime_config import RuntimeConfig

    node22 = _fake_node(tmp_path / "bin" / "node", "v22.23.3")
    _fake_node(tmp_path / ".local/node-v24.21.0-linux-x64/bin/node", "v24.21.0")
    def forbidden(*args, **kwargs):
        pytest.fail("configuration must not discover or execute Node")
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    config = RuntimeConfig.from_env(project_root=ROOT, env={
        "HOME": str(tmp_path), "PATH": str(node22.parent), "SAM_PROFILE": "local",
    })
    assert config.node_bin == ""


@pytest.mark.parametrize("configured", [None, "", "missing-node", "node22"])
def test_runtime_records_explicit_node_without_probing(tmp_path: Path, monkeypatch, configured) -> None:
    from app.runtime_config import RuntimeConfig

    def forbidden(*args, **kwargs):
        pytest.fail("configuration must not execute Node or reject its version")
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    env = {"SAM_PROFILE": "local", "HOME": str(tmp_path), "PATH": ""}
    if configured is not None:
        env["SAM_NODE_BIN"] = str(_fake_node(tmp_path / "node22", "v22.23.3")) if configured == "node22" else configured
    config = RuntimeConfig.from_env(project_root=ROOT, env=env)
    assert config.node_bin == env.get("SAM_NODE_BIN", "")


@pytest.mark.parametrize(("env", "runtime", "expected"), [
    ({}, {}, ""), ({}, {"node_bin": "/managed/node"}, "/managed/node"),
    ({"SAM_NODE_BIN": " raw-node "}, {"node_bin": "/managed/node"}, " raw-node "),
    ({"SAM_NODE_BIN": ""}, {"node_bin": "/managed/node"}, ""),
])
def test_select_node_bin_only_records_values(monkeypatch, env, runtime, expected):
    from app.runtime_config import _select_node_bin
    def forbidden(*args, **kwargs):
        pytest.fail("Node selection must only record configuration")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    assert _select_node_bin(env, runtime) == expected


def test_preflight_failure_stops_standard_startup_before_flask(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    fake = tmp_path / "python3"
    fake.write_text('''#!/bin/sh
printf "%s\\n" "$*" >> "$CALLS"
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "scripts/prepare-openclaw-runtime.py" ]; then exec "$TEST_PYTHON" "$@"; fi
exit 99
''')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + ":/usr/bin:/bin", HOME=str(tmp_path),
               SAM_PROFILE="local", SAM_DATA_ROOT=str(tmp_path / "data"),
               SAM_LEADER_MODEL_API_KEY="synthetic", TEST_PYTHON=sys.executable,
               CALLS=str(calls), SAM_NODE_BIN="/absent/unmanaged-node",
               SAM_OPENCLAW_ENTRY=str(tmp_path / "managed/node_modules/openclaw/openclaw.mjs"))
    # Missing installation fails before even a managed Node probe is possible.
    result = subprocess.run(["bash", "scripts/run.sh"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "OpenClaw runtime preflight failed:" in result.stderr
    lines = calls.read_text().splitlines()
    assert len(lines) == 2
    assert lines[-1] == "scripts/prepare-openclaw-runtime.py"
    assert "webapp/app.py" not in calls.read_text()
