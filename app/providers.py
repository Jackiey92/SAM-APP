"""Composition root for replaceable SAM providers.

Application modules import these factories and the protocols in ``ports``;
only this module knows which local/remote adapter is selected by profile.
"""
from __future__ import annotations

import os
from threading import RLock

from .ports import MemoryProvider, ModelProvider, RuntimeProvider
from .model_provider import OpenAICompatibleProvider
from .runtime_config import RuntimeConfig
from .null_memory import NullMemoryProvider
from .memory.provider import LocalVectorMemoryProvider
from .harness.runtime.openclaw_adapter import OpenClawAdapter
from .harness.staging import StagingStore


def runtime_provider(config: RuntimeConfig, staging: StagingStore) -> RuntimeProvider:
    return OpenClawAdapter(staging, config=config)


def memory_provider(config: RuntimeConfig, *, env: dict[str, str] | None = None) -> MemoryProvider:
    values = env if env is not None else os.environ
    if config.cloud_readonly:
        return NullMemoryProvider(memory_root=config.memory_root_uri)
    backend = values.get("SAM_MEMORY_BACKEND", "localvector")
    if backend == "ov":
        raise ValueError("OV 已摘除；SAM_MEMORY_BACKEND=ov 不再支持，请移除遗留配置")
    if backend != "localvector":
        raise ValueError("SAM_MEMORY_BACKEND must be localvector")
    return LocalVectorMemoryProvider(config.main_db)


def model_provider(config: RuntimeConfig, *, env: dict[str, str] | None = None) -> ModelProvider:
    return OpenAICompatibleProvider(env=env, config=config)


class LazyMemoryProvider:
    """Tool navigation needs no DB/model; defer storage until a memory operation."""
    supports_session = False
    supports_runtime_company_listing = False
    supports_structured_filters = True

    def __init__(self, config, *, env=None, company_id=None, state=None):
        values = env if env is not None else os.environ
        backend = values.get("SAM_MEMORY_BACKEND", "localvector")
        if not config.cloud_readonly and backend == "ov":
            raise ValueError("OV 已摘除；SAM_MEMORY_BACKEND=ov 不再支持，请移除遗留配置")
        if not config.cloud_readonly and backend != "localvector":
            raise ValueError("SAM_MEMORY_BACKEND must be localvector")
        self.config, self.env, self.company_id = config, dict(values), company_id
        self.memory_root = "sam://facts"
        self._state = state if state is not None else [None, RLock()]

    def scoped(self, company_id):
        from .memory.paths import identifier
        company = identifier(company_id)
        if self.company_id is not None and self.company_id != company:
            raise ValueError("company differs from injected host scope")
        return LazyMemoryProvider(self.config, env=self.env, company_id=company, state=self._state)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        with self._state[1]:
            if self._state[0] is None:
                self._state[0] = memory_provider(self.config, env=self.env)
        provider = self._state[0]
        if self.company_id is not None and hasattr(provider, 'scoped'):
            provider = provider.scoped(self.company_id)
        return getattr(provider, name)
