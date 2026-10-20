"""Environment-owned execution budgets, independent of employee decisions."""
from __future__ import annotations

import math
import os

LARGE_SKILLS = {"fact-brief", "fact-guide"}


def seconds(value, env: str, default: float) -> float:
    result = float(value if value is not None else os.environ.get(env, default))
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{env} must be finite and nonnegative")
    return result


def call_timeout(skill: str | None = None, explicit=None) -> int:
    """Explicit caller > per-skill env > global env > skill default."""
    key = "SAM_EMPLOYEE_" + (skill or "").upper().replace("-", "_") + "_TIMEOUT"
    value = explicit if explicit is not None else os.environ.get(key)
    result = seconds(value, "SAM_EMPLOYEE_TIMEOUT", 300 if skill in LARGE_SKILLS else 240)
    if result < 1:
        raise ValueError(f"{key if value is not None else 'SAM_EMPLOYEE_TIMEOUT'} must be >= 1")
    return int(result)


def employee_total_timeout(explicit=None) -> float:
    # Three 300s attempts plus backoff/transport overhead; still a hard cap.
    return seconds(explicit, "SAM_EMPLOYEE_TOTAL_TIMEOUT", 960)


def parse_total_timeout() -> float:
    return max(1, seconds(None, "SAM_PARSE_TOTAL_TIMEOUT", 900))


def openclaw_timeout_floor() -> int:
    # Parser/LEADER calls also use this runtime (600s). --timeout supplies
    # the actual per-call bound; this config must not impose a lower default.
    return max(600, int(seconds(None, "SAM_OPENCLAW_TIMEOUT", 600)),
               *(call_timeout(skill) for skill in (
                   None, "document-classifier", "finance-fact-extraction", *LARGE_SKILLS)))
