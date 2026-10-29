"""Portable SAM runtime configuration.

The repository carries a small YAML manifest and local/cloud profiles. A
dependency-free parser is used so deployment needs no package just to discover
paths and environment-variable names. Environment variables are final wins.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROCESS_STOP_GRACE_SECONDS = 10.0
PROCESS_STOP_POLL_SECONDS = 0.1
OPENCLAW_STATE_DIR_ENV = "SAM_OPENCLAW_STATE_DIR"


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value[0:1] in {"\"", "'"} and value[-1:] == value[0]:
        return value[1:-1]
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small nested-mapping subset used by SAM profiles."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, sep, value = raw.strip().partition(":")
        if not sep or not key:
            raise ValueError(f"unsupported manifest line: {raw!r}")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_config(root: Path, env: dict[str, str]) -> dict[str, Any]:
    manifest_path = Path(env.get("SAM_MANIFEST", root / "sam-manifest.yaml"))
    manifest = _simple_yaml(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    profile_name = env.get("SAM_PROFILE", "local")
    profile_path = Path(env.get("SAM_PROFILE_PATH", root / "profiles" / f"{profile_name}.yaml"))
    profile = _simple_yaml(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
    return _merge(manifest, profile)


def _path(root: Path, value: str | None, default: Path, *, follow_symlinks: bool = True) -> Path:
    if not value:
        candidate = default
    else:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
    # Executable paths must retain virtual-environment symlinks. Calling
    # resolve() on .venv/bin/python turns it into /usr/bin/python3 and loses
    # the venv's sys.prefix and installed packages.
    if follow_symlinks:
        return candidate.resolve()
    return Path(os.path.abspath(candidate))


def _select_node_bin(env: dict[str, str], runtime: dict[str, Any]) -> str:
    """Record the Node path without probing or rejecting a missing executable.

    Version, platform, architecture, provenance and integrity are all checked
    by the read-only preflight. An empty path selects the ledger-managed Node.
    """
    explicit = env.get("SAM_NODE_BIN")
    if explicit is not None:
        return explicit
    configured = runtime.get("node_bin")
    return str(configured) if configured else ""


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    harness_root: Path
    node_bin: str
    openclaw_entry: Path
    venv_bin: Path
    state_dir: Path
    config_path: Path
    data_root: Path
    objects_dir: Path
    main_db: Path
    app_db: Path
    memory_root: Path
    skill_root: Path
    git_dir: str | None
    skill_for_format: dict[str, str]
    model_api: str
    model_base_url: str
    model_base_url_env: str
    model_name_env: str
    model_api_key_env: str
    model_default: str
    memory_provider: str
    memory_root_uri: str
    tool_plugin_dir: Path
    tool_bridge_python: Path
    agent_id: str
    openclaw_mode: str
    gateway_port: int
    cloud_readonly: bool = False

    def for_extraction(self, env: dict[str, str] | None = None) -> "RuntimeConfig":
        """Extraction defaults to Flash without changing the leader model."""
        from dataclasses import replace
        env = env if env is not None else os.environ
        return replace(self, model_default=env.get(self.model_name_env, "qwen3.8-flash"))

    @property
    def two_a_root(self) -> Path:
        return self.objects_dir.parent

    @property
    def maps_dir(self) -> Path:
        return self.two_a_root / "map"

    @classmethod
    def from_env(cls, project_root: str | Path | None = None, env: dict[str, str] | None = None) -> "RuntimeConfig":
        env = env if env is not None else os.environ
        root = Path(project_root or env.get("SAM_PROJECT_ROOT", Path(__file__).resolve().parent.parent)).resolve()
        config = _read_config(root, env)
        cloud_readonly = env.get("SAM_CLOUD_READONLY") == "1"
        memory_root_uri = "sam://facts"
        runtime = config.get("runtime", {})
        paths = config.get("paths", {})
        root_key = "cloud_data_root" if cloud_readonly else "data_root"
        data_root_value = env.get("SAM_DATA_ROOT", paths.get(root_key))
        if not data_root_value:
            raise RuntimeError(f"Set SAM_DATA_ROOT or paths.{root_key} in an explicit profile")
        configured_root = _path(root, env.get("SAM_PROJECT_ROOT", paths.get("project_root")), root)
        skills = config.get("skills", {})
        model = config.get("model", {})
        harness = _path(configured_root, env.get("SAM_HARNESS_ROOT", paths.get("harness_root")), configured_root / "harness-openclaw")
        node_bin = "" if cloud_readonly else _select_node_bin(env, runtime)
        entry = _path(configured_root, env.get("SAM_OPENCLAW_ENTRY", paths.get("openclaw_entry")), configured_root / ".oc-runtime/node_modules/openclaw/openclaw.mjs", follow_symlinks=False)
        venv_bin = _path(configured_root, env.get("SAM_VENV_BIN", paths.get("venv_bin")), configured_root / ".venv/bin", follow_symlinks=False)
        state = _path(configured_root, env.get("SAM_OPENCLAW_STATE_DIR", paths.get("state_dir")), harness / "state")
        config_path = _path(configured_root, env.get("SAM_OPENCLAW_CONFIG", paths.get("config_path")), state / "openclaw.json")
        plugin_dir = _path(configured_root, env.get("SAM_TOOL_PLUGIN_DIR", paths.get("tool_plugin_dir")), harness / "plugins" / "sam-memory")
        # The web/runtime venv intentionally remains separate from the parser
        # venv.  A clean checkout may have Docling in ``.venv`` but CPU Torch
        # only in ``.sam-isolated/venv``; prefer the latter for the bridge when
        # no explicit interpreter was injected.  Explicit configuration always
        # wins, so deployments can use their own isolated interpreter.
        configured_bridge = env.get("SAM_TOOL_BRIDGE_PYTHON")
        manifest_bridge = paths.get("tool_bridge_python")
        if configured_bridge is None and manifest_bridge in {None, ".venv/bin/python"}:
            isolated_bridge = configured_root / ".sam-isolated" / "venv" / "bin" / "python"
            if isolated_bridge.is_file():
                bridge_python = _path(configured_root, str(isolated_bridge), venv_bin / "python", follow_symlinks=False)
            else:
                bridge_python = _path(configured_root, manifest_bridge, venv_bin / "python", follow_symlinks=False)
        else:
            bridge_python = _path(configured_root, configured_bridge or manifest_bridge, venv_bin / "python", follow_symlinks=False)
        agent_id = str(env.get("SAM_OPENCLAW_AGENT", runtime.get("agent_id", "sam-leader")))
        mapping = dict(skills.get("map", {"*": "document-ingest"}))
        raw_mapping = env.get("SAM_SKILL_MAP")
        if raw_mapping:
            loaded = json.loads(raw_mapping)
            if not isinstance(loaded, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()):
                raise ValueError("SAM_SKILL_MAP must be a JSON object of format to skill names")
            mapping = loaded
        base_url_env = str(env.get("SAM_MODEL_BASE_URL_ENV", model.get("base_url_env", "SAM_LEADER_MODEL_BASE_URL")))
        name_env = str(env.get("SAM_MODEL_NAME_ENV", model.get("model_env", "SAM_LEADER_MODEL")))
        key_env = str(env.get("SAM_MODEL_API_KEY_ENV", model.get("api_key_env", "SAM_LEADER_MODEL_API_KEY")))
        # Keep older deployments working while making the leader-scoped names
        # canonical. A new variable always wins over its legacy counterpart.
        legacy_envs = {
            "SAM_LEADER_MODEL_BASE_URL": "SAM_GUIDE_MODEL_BASE_URL",
            "SAM_LEADER_MODEL": "SAM_GUIDE_MODEL",
            "SAM_LEADER_MODEL_API_KEY": "SAM_GUIDE_MODEL_API_KEY",
        }
        for current, legacy in legacy_envs.items():
            if not env.get(current) and env.get(legacy):
                env[current] = env[legacy]
        gateway_mode = str(env.get("SAM_OPENCLAW_MODE", runtime.get("gateway_mode", "gateway")))
        # The web service uses PORT (18789 in the demo).  Never fall back to
        # that listener for the resident OpenClaw gateway when a custom
        # manifest omits its explicit port.
        gateway_port = int(env.get(
            "SAM_OPENCLAW_GATEWAY_PORT",
            env.get("OPENCLAW_GATEWAY_PORT", runtime.get("gateway_port", 18790)),
        ))
        # Cloud snapshots have a separate explicitly configured root; never
        # borrow the local profile's ledger or invent a writable /tmp default.
        local_data = _path(configured_root, data_root_value, configured_root)
        cloud_data = local_data
        def data_path(name: str, key: str, local: Path, cloud: Path) -> Path:
            return _path(configured_root, env.get(name) if cloud_readonly else env.get(name, None if "SAM_DATA_ROOT" in env else paths.get(key)),
                         cloud if cloud_readonly else local)

        two_a_root = data_path("SAM_2A_ROOT", "two_a_root",
                                local_data / "2a", cloud_data / "2a")

        objects_value = env.get("SAM_OBJECTS_DIR")
        if objects_value is None and not cloud_readonly and "SAM_2A_ROOT" not in env:
            objects_value = paths.get("objects_dir")  # legacy explicit override
        objects_dir = _path(configured_root, objects_value,
                            cloud_data / "objects" if cloud_readonly else two_a_root / "bin")

        return cls(
            configured_root, harness, node_bin, entry, venv_bin, state, config_path,
            data_path("SAM_DATA_ROOT", "data_root", local_data, cloud_data),
            objects_dir,
            data_path("SAM_MAIN_DB", "main_db", local_data / "app.db", cloud_data / "app.db"),
            data_path("SAM_APP_DB", "app_db", local_data / "sales_app" / "app.db", cloud_data / "sales_app" / "app.db"),
            data_path("SAM_MEMORY_ROOT", "memory_root", local_data / "memory", cloud_data / "disabled-memory"),
            _path(configured_root, env.get("SAM_SKILL_ROOT", skills.get("root")), configured_root / "skills"),
            env.get("SAM_GIT_DIR"), mapping,
            str(model.get("api", "openai-completions")),
            str(env.get(base_url_env, model.get("base_url", ""))), base_url_env, name_env, key_env,
            str(env.get(name_env, model.get("default_model", "auto"))),
            "null" if cloud_readonly else "localvector",
            memory_root_uri,
            plugin_dir, bridge_python,
            agent_id,
            gateway_mode,
            gateway_port,
            cloud_readonly,
        )
