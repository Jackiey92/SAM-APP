"""Stateless employee model tool: one supplied skill, one request, no agent loop.

Business instructions are loaded by EmployeeRunner from SKILL.md, not defined
here. This tool never calls other tools or shares chat history.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
import urllib.request
from urllib.error import HTTPError

from ..runtime_errors import EmployeeTransportError, transport_exception
from ..ports import ModelUnavailable


class EmployeeModelTool:
    def __init__(self, config):
        self.config = config

    def execute(self, *, skill_text, text, timeout, model=None, images=None):
        # A disposable process makes timeout a wall-clock cap even if an HTTP
        # stream drips bytes forever; secrets/prompts are never CLI arguments.
        payload = {"skill_text": skill_text, "text": text, "timeout": timeout, "model": model, "images": images,
                   "transport_config": {name: getattr(self.config, name) for name in
                       ("model_base_url", "model_base_url_env", "model_default", "model_name_env", "model_api_key_env")}}
        proc = subprocess.run(
            [sys.executable, "-m", "app.employees.model_tool"],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True,
            text=True, timeout=timeout, cwd=self.config.project_root)
        try:
            response = json.loads(proc.stdout)
        except (TypeError, ValueError) as exc:
            raise EmployeeTransportError(f"employee model tool process failed exit={proc.returncode}") from exc
        if "error" in response:
            raise EmployeeTransportError(response["error"], retryable=response.get("retryable", False))
        if proc.returncode or "raw" not in response:
            raise EmployeeTransportError(f"employee model tool process failed exit={proc.returncode}")
        return response["raw"]

    def _request(self, *, skill_text, text, timeout, model=None, images=None):
        config = self.config
        key = os.environ.get(config.model_api_key_env) or os.environ.get("SAM_GUIDE_MODEL_API_KEY")
        if not key:
            raise ModelUnavailable(f"employee model tool requires {config.model_api_key_env}")
        model = model or os.environ.get(config.model_name_env, config.model_default)
        # Gateway fallback IDs are provider-qualified; this tool already owns
        # the configured endpoint, so only accept its provider's model IDs.
        if model.startswith("token-plan/"):
            model = model[len("token-plan/"):]
        elif "/" in model:
            raise ValueError("bounded employee fallback must use the configured endpoint; cross-provider IDs are unsupported")
        content = text
        if images:
            if not isinstance(images, list) or len(images) > 4 or any(
                not isinstance(url, str) or not url.startswith('data:image/png;base64,') or len(url) > 16 * 1024 * 1024
                for url in images
            ):
                raise ValueError('invalid task snapshot images')
            content = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": url}} for url in images]
        body = {"model": model, "messages": [
            {"role": "system", "content": skill_text},
            {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "max_tokens": int(os.environ.get("SAM_EMPLOYEE_MAX_OUTPUT_TOKENS", "16384"))}
        if body["max_tokens"] < 1:
            raise ValueError("SAM_EMPLOYEE_MAX_OUTPUT_TOKENS must be positive")
        # Match the existing Token Plan employee runtime: no hidden reasoning
        # stream. Other compatible endpoints keep their own supported params.
        base_url = os.environ.get(config.model_base_url_env, config.model_base_url).rstrip("/")
        if "token-plan." in base_url:
            body["enable_thinking"] = False
        trace = None
        trace_root = os.environ.get('SAM_MODEL_TRACE_ROOT')
        if trace_root:
            data_root = Path(os.environ['SAM_DATA_ROOT']).resolve()
            folder = Path(trace_root).resolve()
            if not folder.is_relative_to(data_root):
                raise ValueError('model trace must be below explicit isolated data root')
            folder.mkdir(parents=True, exist_ok=True)
            trace = folder / uuid.uuid4().hex
            def record(suffix, data):
                path = trace.with_suffix(suffix)
                with path.open('x', encoding='utf-8') as handle:
                    os.chmod(path, 0o600)
                    handle.write(data)
            record('.request.json', json.dumps(body, ensure_ascii=False))
        started = time.monotonic()
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw_envelope = response.read().decode()
                if trace:
                    record('.response.json', raw_envelope)
                envelope = json.loads(raw_envelope)
            choice = envelope["choices"][0]
            stop = choice.get("finish_reason")
            content = choice["message"].get("content")
            if trace:
                record('.metrics.json', json.dumps({'elapsed_seconds':time.monotonic()-started,
                    'stream':body.get('stream', False), 'finish_reason':stop,
                    'usage':envelope.get('usage'), 'request_bytes':len(json.dumps(body, ensure_ascii=False).encode()),
                    'response_bytes':len(raw_envelope.encode())}))
            if stop == "length" or not content:
                raise EmployeeTransportError(f"employee model={model} stopReason={stop}: empty or truncated response")
            return content
        except HTTPError as exc:
            # Preserve provider parameter/auth diagnostics as well as the code.
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise EmployeeTransportError(f"HTTP {exc.code}: {exc.reason}: {detail}",
                                         retryable=exc.code in {408, 429} or 500 <= exc.code < 600) from exc
        except Exception as exc:
            failure = transport_exception(exc)
            if failure is not None:
                raise failure from exc
            if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
                raise EmployeeTransportError(f"employee model={model}: invalid model response envelope") from exc
            raise


if __name__ == "__main__":
    from ..runtime_config import RuntimeConfig
    from ..runtime_errors import compact_error
    try:
        values = json.load(sys.stdin)
        from dataclasses import replace
        config = replace(RuntimeConfig.from_env(), **values.pop("transport_config"))
        raw = EmployeeModelTool(config)._request(**values)
        print(json.dumps({"raw": raw}, ensure_ascii=False))
    except Exception as exc:
        failure = transport_exception(exc)
        print(json.dumps({"error": compact_error(failure or exc),
                          "retryable": failure.retryable if failure is not None else False}))
        sys.exit(1)
