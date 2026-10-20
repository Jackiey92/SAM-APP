"""Transport-only failure classification; never interpret employee decisions."""
from __future__ import annotations

import errno
import http.client
import socket
import ssl
import json
import re
import subprocess
from urllib.error import HTTPError, URLError


class EmployeeTransportError(RuntimeError):
    """No employee decision was received (distinct from ambiguous/empty)."""

    error_kind = "employee_transport"

    def __init__(self, detail: str, *, retryable: bool = True):
        super().__init__(compact_error(detail))
        self.retryable = retryable


def compact_error(detail: str) -> str:
    detail = re.sub(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)\S+", r"\1<redacted>", str(detail))
    detail = re.sub(r'(?i)(["\']api[_-]?key["\']\s*:\s*["\'])[^"\']+', r"\1<redacted>", detail)
    return " ".join(detail.split())[:1200]


def deterministic_text(detail: str) -> bool:
    # Transport diagnostics only. All definite 4xx except timeout/rate-limit.
    return bool(re.search(
        r"(?i)\b(?:HTTP\s*|status(?:code)?[= :]+)(?!(?:408|429)\b)4\d\d\b|"
        r"unauthorized|invalid api.?key|authentication failed|invalid (?:parameter|schema)|"
        r"schema validation|bad request", detail))


def transient_text(detail: str) -> bool:
    # Only called on CLI/transport error diagnostics, NOT successful model text.
    if deterministic_text(detail):
        return False
    return bool(re.search(
        r"(?i)timed?\s*out|timeout|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|"
        r"connection (?:reset|refused)|socket hang up|fetch failed|"
        r"agent couldn[’']t generate a response|rate.?limit|too many requests|\b429\b|"
        r"connection (?:closed|aborted|interrupted)|unexpected eof|"
        r"empty response|no payloads|stopReason[= :]+length|model-fallback reason=format|"
        r"\b(?:HTTP\s*|status(?:code)?[= :]+)(?:5\d\d|408)\b|"
        r"\b(?:502|503|504)\b|internal server error|service unavailable|bad gateway",
        detail,
    ))


def transport_exception(exc: Exception) -> EmployeeTransportError | None:
    if isinstance(exc, EmployeeTransportError):
        return exc
    if isinstance(exc, HTTPError):
        return EmployeeTransportError(f"HTTP {exc.code}: {exc.reason}", retryable=exc.code in {408, 429} or 500 <= exc.code < 600)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        # TimeoutExpired.__str__ includes the entire command/prompt. Do not persist it.
        return EmployeeTransportError("employee transport timeout")
    if isinstance(exc, (ConnectionError, http.client.RemoteDisconnected, http.client.IncompleteRead, ssl.SSLEOFError)):
        return EmployeeTransportError(f"employee network interrupted: {exc}")
    if isinstance(exc, socket.gaierror) and exc.errno == socket.EAI_AGAIN:
        return EmployeeTransportError(f"employee DNS temporarily unavailable: {exc}")
    if isinstance(exc, URLError):
        return EmployeeTransportError(f"employee network error: {exc.reason}")
    if isinstance(exc, OSError) and exc.errno in {
        errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED, errno.ETIMEDOUT, errno.EPIPE,
        errno.ENETUNREACH, errno.EHOSTUNREACH,
    }:
        return EmployeeTransportError(f"employee network error: {exc}")
    if isinstance(exc, RuntimeError) and (transient_text(str(exc)) or deterministic_text(str(exc))):
        return EmployeeTransportError(str(exc), retryable=transient_text(str(exc)))
    return None


def check_transport_envelope(raw, *, require_response: bool = False) -> None:
    """Inspect known OpenClaw metadata only, without walking candidate content."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if require_response:
            raise EmployeeTransportError("employee empty response")
        return
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return
    if not isinstance(raw, dict) or {"candidates", "items"}.intersection(raw):
        return
    status = raw.get("status")
    payloads = raw.get("result", {}).get("payloads", []) if isinstance(raw.get("result"), dict) else raw.get("payloads", [])
    errors = [item.get("text", "") for item in payloads
              if isinstance(item, dict) and item.get("isError") is True] if isinstance(payloads, list) else []
    result = raw.get("result") if isinstance(raw.get("result"), dict) else raw
    metadata = {key: result.get(key, raw.get(key)) for key in
                ("stopReason", "httpStatus", "statusCode", "runId", "sessionId")
                if result.get(key, raw.get(key)) is not None}
    detail = str(errors[0] if errors else raw.get("error") or result.get("error") or raw.get("summary") or status)
    diagnostic = "OpenClaw error: " + json.dumps(metadata) + " " + detail
    code = metadata.get("httpStatus", metadata.get("statusCode"))
    code = int(code) if str(code).isdigit() else None
    if code is not None and code >= 400:
        raise EmployeeTransportError(diagnostic, retryable=code in {408, 429} or 500 <= code < 600)
    if status == "timeout" or raw.get("timeoutPhase"):
        raise EmployeeTransportError(
            f"OpenClaw status={status} timeoutPhase={raw.get('timeoutPhase', 'unknown')}: "
            + diagnostic,
        )
    if status in {"error", "failed", "aborted"} or errors:
        raise EmployeeTransportError(diagnostic, retryable=transient_text(diagnostic))
    if result.get("payloads") == [] or metadata.get("stopReason") == "length":
        raise EmployeeTransportError("OpenClaw empty or truncated response: " + json.dumps(metadata))
