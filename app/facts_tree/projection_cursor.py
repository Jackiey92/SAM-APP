"""Deterministic, process-keyed opaque cursors with no business dependencies."""
import base64
import binascii
import hmac
import json

_CURSOR_KEY = None


class CursorError(ValueError):
    """Malformed, tampered, or unverifiable cursor; or key misconfiguration."""


def set_cursor_key(key: bytes) -> None:
    """Install the process-lifetime signing key without persisting it."""
    global _CURSOR_KEY
    if type(key) is not bytes or not key:
        raise ValueError("cursor key must be nonempty bytes")
    if _CURSOR_KEY is not None and not hmac.compare_digest(_CURSOR_KEY, key):
        raise ValueError("cursor key already configured")
    _CURSOR_KEY = key


def has_cursor_key() -> bool:
    return _CURSOR_KEY is not None


def require_cursor_key() -> None:
    if not has_cursor_key():
        raise CursorError("cursor key not configured")


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value):
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _b64(raw) != value:
        raise ValueError("noncanonical base64")
    return raw


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("invalid JSON constant")


def encode_cursor(payload: dict) -> str:
    """Sign a nonempty dictionary of scalar JSON values in canonical form."""
    require_cursor_key()
    try:
        if (type(payload) is not dict or not payload
                or any(type(key) is not str or not key for key in payload)
                or any(type(value) not in (str, int, bool, type(None))
                       for value in payload.values())):
            raise ValueError("shape")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return _b64(raw) + "." + _b64(hmac.digest(_CURSOR_KEY, raw, "sha256"))
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise CursorError("invalid page cursor") from None


def decode_cursor(token, *, expected_keys=None) -> dict:
    """Verify a cursor and optionally require an exact set of payload keys."""
    require_cursor_key()
    try:
        if type(token) is not str:
            raise ValueError("token type")
        payload, signature = token.split(".")
        raw, mac = _unb64(payload), _unb64(signature)
        if not hmac.compare_digest(hmac.digest(_CURSOR_KEY, raw, "sha256"), mac):
            raise ValueError("signature")
        data = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        if type(data) is not dict:
            raise ValueError("shape")
        if expected_keys is not None:
            if (type(expected_keys) not in (frozenset, set, tuple) or not expected_keys
                    or any(type(key) is not str or not key for key in expected_keys)
                    or set(data) != set(expected_keys)):
                raise ValueError("keys")
        return data
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise CursorError("invalid page cursor") from None
