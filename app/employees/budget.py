"""Host-owned hard limits for bounded attribution model-tool execution."""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import time

from ..employee_policy import seconds
from ..runtime_errors import EmployeeTransportError


class EmployeeBudgetExceeded(EmployeeTransportError):
    error_kind = "employee_budget"

    def __init__(self, detail):
        super().__init__(detail, retryable=False)


@dataclass
class AttributionBudget:
    max_calls: int = field(default_factory=lambda: int(os.environ.get("SAM_ATTRIBUTION_MAX_MODEL_CALLS", "12")))
    timeout: float = field(default_factory=lambda: seconds(None, "SAM_ATTRIBUTION_TOTAL_TIMEOUT", 600))
    model_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        if self.max_calls < 1 or self.timeout < 1:
            raise ValueError("employee model/time budgets must be positive")

    @property
    def deadline(self):
        return self.started + self.timeout

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining < 1:
            raise EmployeeBudgetExceeded(f"employee time budget exhausted: model_calls={self.model_calls} timeout={self.timeout}s")
        return remaining

    def check(self):
        self.remaining()
        if self.model_calls >= self.max_calls:
            raise EmployeeBudgetExceeded(f"employee model call budget exhausted: model_calls={self.model_calls} limit={self.max_calls}")

    def consume(self):
        self.check()
        self.model_calls += 1


# Context-local to one parse worker (no cross-company or concurrent-job sharing).
CURRENT_PARSE_BUDGET = ContextVar("sam_parse_model_budget", default=None)


@contextmanager
def parse_model_budget():
    from ..employee_policy import parse_total_timeout
    budget = AttributionBudget(max_calls=int(os.environ.get("SAM_PARSE_MAX_MODEL_CALLS", "64")),
                               timeout=parse_total_timeout())
    token = CURRENT_PARSE_BUDGET.set(budget)
    try:
        yield budget
    finally:
        CURRENT_PARSE_BUDGET.reset(token)
