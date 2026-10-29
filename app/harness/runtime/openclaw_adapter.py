"""OpenClaw harness adapter (thin, replaceable).

Drives the locally installed OpenClaw CLI to run a registered parser skill,
then collects the confined outbox result and routes it to the existing staging
store. The rest of the app depends on this adapter's interface, not on
OpenClaw itself, so the runtime can be swapped later.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from threading import Lock
from collections.abc import Callable

from ..contracts import ParseResult, TextSpan, TableRow, SourceLoc, ClassificationHint
from ..staging import StagingStore
from ...ports import RetryableRuntimeError, RuntimeProvider
from ...runtime_config import (
    RuntimeConfig, OPENCLAW_STATE_DIR_ENV,
    PROCESS_STOP_GRACE_SECONDS, PROCESS_STOP_POLL_SECONDS,
)
from ...employee_policy import openclaw_timeout_floor
from ...runtime_contract import EngineProof, RuntimeContractError, preflight_runtime
from ...runtime_errors import EmployeeTransportError, check_transport_envelope, transport_exception, transient_text


_READONLY_TOOLS = [
    "sam_memory_read", "sam_memory_search", "sam_file_get", "sam_memory_map",
    "sam_conversation_read", "sam_thread_list", "sam_thread_open",
    "sam_document_ingest",
]
_GATEWAY_LOCK = Lock()
_GATEWAY_PROCESSES: dict[str, tuple[subprocess.Popen, object]] = {}


def _scope_session_id(company_id: str | None, thread_id: str | None, nonce: str, *, access_role: str = "employee") -> str:
    """Encode trusted request scope into the OpenClaw session identity.

    The resident Gateway has one process environment and cannot receive a new
    ``SAM_THREAD_ID`` per request.  The plugin decodes this identity from its
    trusted session manager context and applies it only to the short-lived
    bridge child.  Model-supplied tool params never participate.
    """
    if company_id is None or thread_id is None:
        return "oc-" + nonce
    def part(value: str) -> str:
        return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")
    prefix = "sam-manager" if access_role == "manager" else "sam-scope"
    return f"{prefix}.{part(company_id)}.{part(thread_id)}.{nonce}"


def _stop_gateways() -> None:
    with _GATEWAY_LOCK:
        processes = list(_GATEWAY_PROCESSES.values())
        _GATEWAY_PROCESSES.clear()
    for process, stream in processes:
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        try:
            stream.close()
        except AttributeError:
            pass


def _discard_gateway(key: str, process: subprocess.Popen) -> None:
    with _GATEWAY_LOCK:
        current = _GATEWAY_PROCESSES.get(key)
        if current is None or current[0] is not process:
            return
        _GATEWAY_PROCESSES.pop(key, None)
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
    try:
        current[1].close()
    except AttributeError:
        pass


atexit.register(_stop_gateways)


class OpenClawAdapter(RuntimeProvider):
    def __init__(self, staging: StagingStore, *, config: RuntimeConfig | None = None,
                 objects_dir: Path | None = None, db_path: Path | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self.staging = staging
        self.config = config or RuntimeConfig.from_env()
        self.root = self.config.harness_root
        self.objects_dir = Path(objects_dir) if objects_dir else self.config.objects_dir
        self.db_path = Path(db_path) if db_path else self.config.main_db
        self.staging.maps_path = self.objects_dir.parent / "map"
        self._runner = runner or subprocess.run
        self._engine_proof: EngineProof | None = None
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_lock = Lock()

    def _require_engine_proof(self) -> EngineProof:
        """Read-only gate before runtime writes, port reclaim or engine children."""
        if self.config.cloud_readonly:
            raise RuntimeContractError("cloud read-only profile must not start the OpenClaw engine")
        if self._engine_proof is None:
            # An injected runner must also pass the pinned-runtime gate.
            self._engine_proof = preflight_runtime(self.config, runner=self._runner)
        return self._engine_proof

    def _verified_node_bin(self) -> str:
        return self._require_engine_proof().node_bin or self.config.node_bin

    def supports(self, format: str) -> bool:
        return format in self.config.skill_for_format

    def run_parse(self, file_hash: str, format: str, timeout: int = 600, *, quality_issues=None) -> int:
        self._require_engine_proof()
        if not self.supports(format):
            raise KeyError(f"no OpenClaw skill for format: {format}")

        task_path = self._write_task(file_hash, format)
        skill_name = self.config.skill_for_format[format]
        try:
            if quality_issues:
                task = json.loads(task_path.read_text())
                task['quality_issues'] = quality_issues
                task_path.write_text(json.dumps(task, ensure_ascii=False))
            out_path = self.root / "outbox" / f"{file_hash}.json"
            if out_path.exists():
                out_path.unlink()
            message = (
                f'Use the {skill_name} skill for file_hash={file_hash}, format={format}. '
                f'Call the registered sam_document_ingest tool; it converts the source and performs quality '
                f'checks, alternate-engine retry, and returns the complete document conversion. '
                f'Do not use filesystem paths, run shell commands, or write facts, memory, or other application state.'
            )

            # Parsing jobs must not share the scoped leader conversation:
            # each call deliberately uses the unscoped session path, whose
            # nonce creates an independent OpenClaw session per document.
            # This prevents one slow tool_call from serializing other files.
            self._invoke_agent(message, timeout=timeout)

            if not out_path.exists():
                raise RuntimeError(f"OpenClaw produced no output at {out_path}")

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            # document-ingest emits the L2 manifest directly.  Keep it lossless in
            # staging; legacy ParseResult payloads remain supported for migration.
            if isinstance(payload, dict) and "parse_summary" in payload and "pages" in payload:
                return self.staging.save_manifest(payload)
            result = _result_from_dict(payload)
            return self.staging.save_parse(result)
        finally:
            # Task JSON is private handoff state, not a durable user artifact.
            # Removing it here also covers cancellation and bridge timeouts.
            try:
                task_path.unlink()
            except FileNotFoundError:
                pass

    def cancel_parse(self, file_hash: str) -> bool:
        """Keep the RuntimeProvider cancellation seam without owning Agent work."""
        return True

    def clear_parse_cancel(self, file_hash: str) -> None:
        return None

    def run_agent_message(self, message: str, *, context_text: str | None = None,
                          company_id: str | None = None, thread_id: str | None = None,
                          allow_promote: bool = False, timeout: int = 600,
                          access_role: str = "employee") -> str:
        self._require_engine_proof()
        if access_role not in {"employee", "manager"}:
            raise ValueError("invalid runtime access role")
        prompt = message
        if context_text:
            prompt = context_text.rstrip() + "\n\n用户问题：" + message
        return self._invoke_agent(prompt, timeout=timeout, company_id=company_id,
                                  thread_id=thread_id, allow_promote=allow_promote,
                                  access_role=access_role)

    def ensure_runtime_ready(self, *, timeout: int = 90) -> None:
        """Health-probe the owned Gateway before a parse worker starts work."""
        self._require_engine_proof()
        if self.config.openclaw_mode != "gateway":
            return
        self._ensure_gateway(self._base_env(), timeout=timeout)

    def _write_task(self, file_hash: str, format: str) -> Path:
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        task_path = inbox / f"{file_hash}.json"
        from app.storage import ArchiveFileStore
        store = ArchiveFileStore(objects_path=self.objects_dir, db_path=self.db_path)
        stored = store.get(file_hash)
        task_path.write_text(
            json.dumps(
                {
                    "file_hash": file_hash,
                    "format": format,
                    "original_name": stored.original_name,
                    "source_path": str(store.path_for(file_hash)),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return task_path

    def _invoke_agent(self, message: str, timeout: int, *, company_id: str | None = None,
                      thread_id: str | None = None, allow_promote: bool = False,
                      access_role: str = "employee") -> str:
        self._require_engine_proof()
        deadline = time.monotonic() + timeout
        env = self._base_env()
        if self.config.openclaw_mode == "gateway":
            # The Gateway ownership fingerprint includes the effective model
            # configuration, so prepare the child environment first.
            self._ensure_gateway(env, timeout=timeout)
        timeout = int(deadline - time.monotonic())
        if timeout < 1:
            raise EmployeeTransportError("employee deadline exhausted during gateway setup")
        if company_id is not None:
            env["SAM_COMPANY_ID"] = str(company_id)
        if thread_id is not None:
            env["SAM_THREAD_ID"] = str(thread_id)
        env["SAM_ALLOW_PROMOTE"] = "1" if allow_promote else "0"
        # This value is not taken from a model message.  It is only set by the
        # trusted manager entry point and consumed by the plugin's scope hook.
        env["SAM_TOOL_SCOPE"] = "global_read_only" if access_role == "manager" else "company"
        cache_dir = self.root / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        env["XDG_CACHE_HOME"] = str(cache_dir)
        session_id = self._session_id(company_id, thread_id, access_role=access_role)
        cmd = [self._verified_node_bin(), str(self.config.openclaw_entry), "agent",
               "--agent", self.config.agent_id, "--session-id", session_id, "--json",
               "--message", message, "--timeout", str(timeout)]
        if self.config.openclaw_mode != "gateway":
            cmd.insert(3, "--local")
        try:
            proc = self._runner(
                cmd, cwd=self.root, env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except Exception as exc:
            failure = transport_exception(exc)
            if failure is not None:
                raise failure from exc
            raise
        # Some CLI versions emit timeout metadata with exit code zero. Error
        # payloads are not JSON business decisions in either case.
        check_transport_envelope(proc.stdout)
        check_transport_envelope(proc.stderr)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise EmployeeTransportError(
                f"openclaw agent failed ({proc.returncode}): {detail}",
                retryable=transient_text(detail),
            )
        return proc.stdout

    def _base_env(self) -> dict[str, str]:
        """Build trusted runtime environment without request-specific scope."""
        self._require_engine_proof()
        env = os.environ.copy()
        env["PATH"] = f"{self.config.venv_bin}:{env.get('PATH', '')}"
        state_dir = self.config.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        env[OPENCLAW_STATE_DIR_ENV] = str(state_dir)
        env["OPENCLAW_CONFIG_PATH"] = str(self.config.config_path)
        self._ensure_skill_mount()
        env["SAM_PROJECT_ROOT"] = str(self.config.project_root)
        env["SAM_HARNESS_ROOT"] = str(self.config.harness_root)
        env["SAM_SKILL_ROOT"] = str(self.config.skill_root)
        env["OPENCLAW_GATEWAY_PORT"] = str(self.config.gateway_port)
        env["SAM_OPENCLAW_GATEWAY_PORT"] = str(self.config.gateway_port)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.config.project_root), env.get("PYTHONPATH", "")]))
        env["SAM_TOOL_PLUGIN_DIR"] = str(self.config.tool_plugin_dir)
        env["SAM_TOOL_BRIDGE_PYTHON"] = str(self.config.tool_bridge_python)
        env["SAM_OBJECTS_DIR"] = str(self.objects_dir)
        env["SAM_2A_ROOT"] = str(self.objects_dir.parent)
        # RuntimeConfig already resolves environment wins.  The API key is
        # intentionally never synthesized or logged here.
        if self.config.model_base_url:
            env[self.config.model_base_url_env] = self.config.model_base_url
        env[self.config.model_name_env] = self.config.model_default
        if self.config.model_api_key_env not in env and env.get("SAM_GUIDE_MODEL_API_KEY"):
            env[self.config.model_api_key_env] = env["SAM_GUIDE_MODEL_API_KEY"]
        return env

    def _session_id(self, company_id: str | None, thread_id: str | None, *, access_role: str = "employee") -> str:
        if company_id is None or thread_id is None:
            import uuid
            return _scope_session_id(company_id, thread_id, uuid.uuid4().hex, access_role=access_role)
        key = (str(company_id), str(thread_id), access_role)
        with self._session_lock:
            session_id = self._session_ids.get(key)
            if session_id is None:
                import uuid
                session_id = _scope_session_id(company_id, thread_id, uuid.uuid4().hex, access_role=access_role)
                self._session_ids[key] = session_id
            return session_id

    def _ensure_skill_mount(self) -> None:
        """Ensure the configured first-party skill root is visible to OpenClaw."""
        template = self.config.harness_root / "state" / "openclaw.example.json"
        path = self.config.config_path
        config: dict = {}
        if template.exists():
            try:
                loaded = json.loads(template.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = deepcopy(loaded)
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("OpenClaw configuration template is invalid") from exc
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    _merge_missing(config, loaded)
            except (OSError, json.JSONDecodeError):
                # A corrupt runtime config must not erase the known-good
                # template; continue with template defaults and rewrite it.
                pass
        skills = config.setdefault("skills", {})
        loader = skills.setdefault("load", {})
        dirs = loader.setdefault("extraDirs", [])
        if not isinstance(dirs, list):
            dirs = []
            loader["extraDirs"] = dirs
        skill_root = str(self.config.skill_root)
        if skill_root not in dirs:
            dirs.append(skill_root)
        self._ensure_plugin_mount()
        _ensure_plugin_defaults(config)
        _normalize_agent_entries(config)
        _rename_legacy_agent(config)
        _ensure_agent_model_defaults(config)
        defaults = config.setdefault("agents", {}).setdefault("defaults", {})
        # Optional operator-owned fallback, no semantic routing or model guess.
        fallback = os.environ.get("SAM_EMPLOYEE_FALLBACK_MODEL", "").strip()
        if fallback:
            defaults.setdefault("model", {})["fallbacks"] = [fallback]
            for agent in config["agents"].get("entries", {}).values():
                agent.setdefault("model", {})["fallbacks"] = [fallback]
        output_tokens = int(os.environ.get("SAM_EMPLOYEE_MAX_OUTPUT_TOKENS", "16384"))
        if output_tokens < 1:
            raise ValueError("SAM_EMPLOYEE_MAX_OUTPUT_TOKENS must be positive")
        defaults.setdefault("models", {}).setdefault(
            "token-plan/${SAM_LEADER_MODEL}", {}).setdefault("params", {})["maxTokens"] = output_tokens
        configured_timeout = defaults.get("timeoutSeconds")
        # Zero means unlimited in OpenClaw, not a shorter budget. Preserve
        # an operator's larger default; explicit CLI seconds still bound runs.
        if configured_timeout != 0:
            defaults["timeoutSeconds"] = max(openclaw_timeout_floor(), int(configured_timeout or 0))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def prepare_runtime(self) -> None:
        """Prepare a fresh state directory before OpenClaw is started.

        This deliberately does not start Gateway: the daemon must live as a
        child of the long-running Flask process so its lifecycle and atexit
        cleanup are owned by one process.  The first agent call then pays at
        most one bounded Gateway startup cost and all later calls reuse it.
        """
        self._require_engine_proof()
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_skill_mount()

    def _ensure_plugin_mount(self) -> None:
        """Expose the repository plugin through OpenClaw's state discovery dir."""
        source = self.config.tool_plugin_dir
        if not source.is_dir():
            raise RuntimeError("SAM memory plugin directory is missing")
        target = self.config.state_dir / "extensions" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            if target.resolve() == source.resolve():
                return
            target.unlink()
        if target.exists():
            # Do not overwrite an administrator-managed installed copy. The
            # OpenClaw install ledger may already own this exact discovery dir.
            return
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            raise RuntimeError("unable to mount SAM memory plugin") from exc

    def _ensure_gateway(self, env: dict[str, str], *, timeout: int) -> None:
        """Start one owned loopback Gateway per state directory and reuse it.

        A TCP listener alone is not proof that it belongs to this Flask
        process.  After an unclean restart an old Gateway can retain the port
        and silently keep the previous model/configuration.  We persist a
        non-secret owner record with a configuration fingerprint and reclaim
        only a process we can prove is our own; an unknown listener fails
        closed instead of serving requests through the wrong runtime.
        """
        self._require_engine_proof()
        # Unit tests inject a runner and must not launch a real daemon. The
        # production composition root uses subprocess.run and takes this path.
        if self._runner is not subprocess.run:
            return
        key = str(self.config.state_dir)
        with _GATEWAY_LOCK:
            current = _GATEWAY_PROCESSES.get(key)
            if current is not None and current[0].poll() is None:
                # Another request may have started it while this request was
                # waiting for the lock. Reuse that process and wait for its
                # socket instead of launching a second daemon on the port.
                process = current[0]
            else:
                if current is not None:
                    try:
                        current[1].close()
                    except AttributeError:
                        pass
                if self._reclaim_or_reject_gateway_port(env):
                    if not self._gateway_rpc_healthy(env, timeout=timeout):
                        raise RetryableRuntimeError("Owned OpenClaw Gateway is not healthy; refusing to restart it")
                    return
                log_path = self.config.state_dir / "gateway.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                stream = log_path.open("a", encoding="utf-8")
                command = [self._verified_node_bin(), str(self.config.openclaw_entry), "gateway", "run", "--port", str(self.config.gateway_port)]
                process = subprocess.Popen(
                    command, cwd=self.root, env=env, stdout=stream, stderr=stream,
                    text=True,
                )
                _GATEWAY_PROCESSES[key] = (process, stream)
        try:
            configured_limit = max(1.0, float(os.environ.get("SAM_GATEWAY_STARTUP_TIMEOUT", "90")))
        except ValueError:
            configured_limit = 90.0
        deadline = time.monotonic() + min(max(float(timeout), 1.0), configured_limit)
        delay = 0.1
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _discard_gateway(key, process)
                raise RetryableRuntimeError("OpenClaw Gateway exited during startup; parse job will retry")
            try:
                with socket.create_connection(("127.0.0.1", self.config.gateway_port), timeout=0.2):
                    if self._gateway_rpc_healthy(env, timeout=max(1, int(deadline - time.monotonic()))):
                        self._write_gateway_owner(process, env)
                        return
            except OSError:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2.0, 1.5)
        _discard_gateway(key, process)
        raise RetryableRuntimeError("OpenClaw Gateway startup timed out; parse job will retry")

    def reclaim_gateway_port(self) -> None:
        """Validate a resident Gateway without restarting or taking ownership."""
        self._require_engine_proof()
        self._reclaim_or_reject_gateway_port()

    def _gateway_rpc_healthy(self, env: dict[str, str], *, timeout: int) -> bool:
        """TCP alone is insufficient: use the authenticated Gateway health RPC."""
        self._require_engine_proof()
        limit = min(max(timeout, 1), 10)
        try:
            result = self._runner(
                [self._verified_node_bin(), str(self.config.openclaw_entry), "gateway", "health",
                 "--json", "--timeout", str(limit * 1000)],
                cwd=self.root, env=env, capture_output=True, text=True, timeout=limit + 2,
            )
            # Plugin diagnostics may precede the JSON object.
            raw = result.stdout
            start = raw.find("{")
            payload = json.loads(raw[start:]) if start >= 0 else None
            return result.returncode == 0 and isinstance(payload, dict) and payload.get("ok") is True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    @property
    def _gateway_owner_path(self) -> Path:
        return self.config.state_dir / "gateway.owner.json"

    def _gateway_fingerprint(self, env: dict[str, str] | None = None) -> str:
        """Fingerprint effective non-secret Gateway identity/configuration."""
        try:
            config_digest = hashlib.sha256(self.config.config_path.read_bytes()).hexdigest()
        except OSError:
            config_digest = "missing"
        effective_env = env or os.environ
        material = {
            "config_digest": config_digest,
            "entry": str(self.config.openclaw_entry),
            "model": effective_env.get(self.config.model_name_env, self.config.model_default),
            "model_base_url": effective_env.get(self.config.model_base_url_env, self.config.model_base_url),
            "port": self.config.gateway_port,
            "state_dir": str(self.config.state_dir),
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _read_gateway_owner(self) -> dict[str, object] | None:
        try:
            directory = self.config.state_dir.stat()
            if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
                return None
            fd = os.open(self._gateway_owner_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    return None
                payload = json.load(stream)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_gateway_owner(self, process: subprocess.Popen, env: dict[str, str]) -> None:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            # Test doubles need no filesystem ownership record.
            return
        payload = {
            "fingerprint": self._gateway_fingerprint(env),
            "pid": pid,
            "port": self.config.gateway_port,
            "state_dir": str(self.config.state_dir),
            "start_ticks": self._process_start_ticks(pid),
            "uid": os.getuid(),
        }
        self._gateway_owner_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".gateway-owner-", dir=self.config.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
            os.replace(name, self._gateway_owner_path)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _process_start_ticks(pid: int) -> str | None:
        try:
            # comm can contain spaces/parentheses; field 22 follows its LAST ')'.
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None

    def _pid_listens_on_gateway_port(self, pid: int) -> bool:
        """Bind the owner PID to the actual loopback LISTEN socket, not a label."""
        try:
            inodes = set()
            for path in ("/proc/net/tcp", "/proc/net/tcp6"):
                for line in Path(path).read_text().splitlines()[1:]:
                    fields = line.split()
                    address, port = fields[1].split(":")
                    if (int(port, 16) == self.config.gateway_port and fields[3] == "0A"
                            and address in {"0100007F", "00000000000000000000000001000000"}):
                        inodes.add(fields[9])
            return any(os.readlink(fd) in {f"socket:[{inode}]" for inode in inodes}
                       for fd in Path(f"/proc/{pid}/fd").iterdir())
        except (OSError, ValueError, IndexError):
            return False

    def _gateway_port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.config.gateway_port), timeout=0.2):
                return True
        except OSError:
            return False

    def _owner_is_sam_gateway(self, owner: dict[str, object]) -> bool:
        try:
            pid = int(owner.get("pid", 0))
            port = int(owner.get("port", -1))
        except (TypeError, ValueError):
            return False
        if pid <= 0 or port != self.config.gateway_port or owner.get("state_dir") != str(self.config.state_dir):
            return False
        try:
            os.kill(pid, 0)
            proc = Path(f"/proc/{pid}")
            cmdline = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
            if (proc.stat().st_uid != os.getuid() or owner.get("uid") != os.getuid()
                    or not owner.get("start_ticks")
                    or owner["start_ticks"] != self._process_start_ticks(pid)
                    or (proc / "exe").resolve() != Path(self._verified_node_bin()).resolve()
                    or (proc / "cwd").resolve() != self.root.resolve()
                    or not self._pid_listens_on_gateway_port(pid)):
                return False
        except OSError:
            return False
        return self._gateway_cmdline_is_ours(cmdline)

    def _gateway_cmdline_is_ours(self, cmdline: str) -> bool:
        # The daemon rewrites its process title to just "openclaw-gateway"
        # after start, so the launch path is no longer visible in
        # /proc/pid/cmdline. Accept that exact title only alongside either
        # the validated owner record or all five orphan process proofs;
        # the full-launch form is accepted as well.
        # The title string is followed by NUL padding in the raw cmdline.
        if cmdline.replace("\x00", "").strip() == "openclaw-gateway":
            return True
        args = cmdline.split("\x00")
        return str(self.config.openclaw_entry) in args and "gateway" in args and "run" in args

    def _stop_owned_orphan(self, owner: dict[str, object]) -> None:
        pid = int(owner["pid"])
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not self._gateway_port_open():
                return
            # Recheck identity before each signal, including escalation. A PID
            # reused after TERM is not the process we were authorized to kill.
            if (self._process_start_ticks(pid) != owner.get("start_ticks")
                    or not self._orphan_pid_is_sam_gateway(pid)):
                raise RuntimeError("OpenClaw Gateway port is occupied by an unverified process")
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + PROCESS_STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not self._gateway_port_open():
                    return
                time.sleep(PROCESS_STOP_POLL_SECONDS)
        raise RuntimeError("Owned orphan OpenClaw Gateway did not release its port")

    def _orphan_pid_is_sam_gateway(self, pid: int) -> bool:
        """Five independent process proofs; inaccessible /proc fails closed."""
        try:
            proc = Path(f"/proc/{pid}")
            if (proc.stat().st_uid != os.getuid()
                    or (proc / "cwd").resolve(strict=True) != self.root.resolve()
                    or not self._gateway_cmdline_is_ours((proc / "cmdline").read_bytes().decode("utf-8", "replace"))
                    or not self._pid_listens_on_gateway_port(pid)):
                return False
            env = dict(item.split(b"=", 1) for item in (proc / "environ").read_bytes().split(b"\0")
                       if b"=" in item)
            state = env.get(OPENCLAW_STATE_DIR_ENV.encode())
            return (bool(state) and Path(os.fsdecode(state)).is_absolute()
                    and Path(os.fsdecode(state)).resolve() == self.config.state_dir.resolve())
        except (OSError, ValueError, RuntimeError):
            return False

    def _find_orphan_gateway_owner(self) -> dict[str, object] | None:
        # A malformed/unsafe owner file is not a missing one. Never bypass
        # existing owner-file permissions, symlink or fingerprint safeguards.
        try:
            self._gateway_owner_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return None
        else:
            return None
        owners = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            pid = int(proc.name)
            start = self._process_start_ticks(pid)
            if start and self._orphan_pid_is_sam_gateway(pid):
                owners.append({"pid": pid, "start_ticks": start})
        return owners[0] if len(owners) == 1 else None

    def _reclaim_or_reject_gateway_port(self, env: dict[str, str] | None = None) -> bool:
        self._require_engine_proof()
        if not self._gateway_port_open():
            return False
        owner = self._read_gateway_owner()
        if owner is None:
            orphan = self._find_orphan_gateway_owner()
            if orphan is None:
                raise RuntimeError("OpenClaw Gateway port is occupied by an unverified process")
            self._stop_owned_orphan(orphan)
            if self._gateway_port_open():
                raise RuntimeError("Owned orphan OpenClaw Gateway did not release its port")
            return False
        if not self._owner_is_sam_gateway(owner):
            raise RuntimeError("OpenClaw Gateway port is occupied by an unverified process")
        if owner.get("fingerprint") != self._gateway_fingerprint(env):
            raise RuntimeError("Owned OpenClaw Gateway configuration differs; explicit operator restart required")
        # A live SAM gateway whose owner record binds it to this port and
        # state directory is reusable even when a different Flask process
        # spawned it. Do NOT restart it: the spawner may hold secret
        # context that this process cannot reconstruct, and a forced
        # restart would surface as SECRET_SURFACE_UNAVAILABLE.
        return True


def _merge_missing(defaults: dict, existing: dict) -> None:
    """Overlay runtime-specific values while retaining newly added defaults."""
    for key, value in existing.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            _merge_missing(defaults[key], value)
        else:
            defaults[key] = value


def _ensure_agent_model_defaults(config: dict) -> None:
    """Keep an older runtime config from dropping the configured provider."""
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return
    defaults = agents.get("defaults")
    if isinstance(defaults, dict):
        model = defaults.setdefault("model", {})
        if isinstance(model, dict):
            model.setdefault("primary", "token-plan/${SAM_LEADER_MODEL}")
        models = defaults.setdefault("models", {})
        if isinstance(models, dict):
            model_defaults = models.setdefault("token-plan/${SAM_LEADER_MODEL}", {})
            if isinstance(model_defaults, dict):
                params = model_defaults.setdefault("params", {})
                if isinstance(params, dict):
                    params.setdefault("enable_thinking", False)
    entries = agents.get("list")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                model = entry.setdefault("model", {})
                if isinstance(model, dict):
                    model.setdefault("primary", "token-plan/${SAM_LEADER_MODEL}")
    entries_map = agents.get("entries")
    if isinstance(entries_map, dict):
        for entry in entries_map.values():
            if isinstance(entry, dict):
                model = entry.setdefault("model", {})
                if isinstance(model, dict):
                    model.setdefault("primary", "token-plan/${SAM_LEADER_MODEL}")


def _normalize_agent_entries(config: dict) -> None:
    """Emit the 2026.9+ agents.entries shape without legacy id fields."""
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return
    legacy = agents.pop("list", None)
    if not isinstance(legacy, list):
        return
    entries = agents.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        agents["entries"] = entries
    for raw in legacy:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        agent_id = str(entry.pop("id", "") or "").strip()
        if not agent_id:
            name = str(entry.get("name", "agent")).strip().lower().replace(" ", "-")
            agent_id = name or "agent"
        entries.setdefault(agent_id, entry)


def _rename_legacy_agent(config: dict) -> None:
    """Migrate the old internal id while retaining user customizations."""
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return
    entries = agents.get("entries")
    if not isinstance(entries, dict):
        return
    legacy = entries.pop("sam-guide", None)
    leader = entries.setdefault("sam-leader", {})
    if isinstance(legacy, dict) and isinstance(leader, dict):
        _merge_missing(leader, legacy)
        leader["name"] = "SAM Leader"


def _ensure_plugin_defaults(config: dict) -> None:
    plugins = config.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    if isinstance(entries, dict):
        entry = entries.setdefault("sam-memory", {})
        if isinstance(entry, dict):
            entry["enabled"] = True
    tools = config.setdefault("tools", {})
    if isinstance(tools, dict):
        current = tools.get("allow")
        if not isinstance(current, list):
            current = []
        tools["allow"] = _merge_tool_allowlist(current)
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return
    entries_list = agents.get("list")
    if isinstance(entries_list, list):
        for agent in entries_list:
            if isinstance(agent, dict) and isinstance(agent.get("tools"), dict):
                current = agent["tools"].get("allow")
                agent["tools"]["allow"] = _merge_tool_allowlist(current if isinstance(current, list) else [])
    entries_map = agents.get("entries")
    if isinstance(entries_map, dict):
        for agent in entries_map.values():
            if isinstance(agent, dict) and isinstance(agent.get("tools"), dict):
                current = agent["tools"].get("allow")
                agent["tools"]["allow"] = _merge_tool_allowlist(current if isinstance(current, list) else [])


def _merge_tool_allowlist(current: list) -> list[str]:
    result = [str(item) for item in current if str(item) != "sam_promote"]
    for name in _READONLY_TOOLS:
        if name not in result:
            result.append(name)
    return result


def _result_from_dict(d: dict) -> ParseResult:
    hint = ClassificationHint(**d.get("hint", {}))
    spans = [
        TextSpan(
            text=s["text"],
            kind=s.get("kind", "text"),
            loc=SourceLoc(**s["loc"]),
        )
        for s in d.get("text_spans", [])
    ]
    rows = [
        TableRow(
            headers=r["headers"],
            values=r["values"],
            sheet=r.get("sheet"),
            row_index=r.get("row_index"),
            loc=SourceLoc(**r["loc"]),
        )
        for r in d.get("table_rows", [])
    ]
    return ParseResult(
        file_hash=d["file_hash"],
        original_name=d["original_name"],
        format=d["format"],
        text_spans=spans,
        table_rows=rows,
        hint=hint,
    )
