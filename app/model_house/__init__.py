"""04 设计在 app 层的最小落地，本期只覆盖 embedding 模态。

不重构思考模型 model: 配置，不建 IRSS 账本/调度，不做自动发现。
新增模型只能由宿主手工编辑 embeddings.json（受控配置动作）。
"""
from .embedding_catalog import (
    EmbeddingCatalogError, EmbeddingSpec, load_embedding_catalog,
    resolve_embedding, resolve_embedding_spec,
)

__all__ = [
    "EmbeddingCatalogError", "EmbeddingSpec", "load_embedding_catalog",
    "resolve_embedding", "resolve_embedding_spec",
]
