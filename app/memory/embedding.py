"""Offline-by-default embedding; no runtime download or implicit remote fallback."""
from pathlib import Path
from typing import Protocol

MODEL = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class EmbeddingProvider(Protocol):
    dim: int
    embedder: str
    model_version: str

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class LocalBgeProvider:
    """Pinned M0 CPU model and cache, always local_files_only (even online)."""
    embedder = "local-bge"

    def __init__(self, *, cache_dir: str | Path | None = None):
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModel, AutoTokenizer

        cache = cache_dir or Path(__file__).resolve().parents[2] / "poc/memory_m0/model-cache"
        snapshot = snapshot_download(
            MODEL, cache_dir=str(cache), revision=REVISION, local_files_only=True,
            allow_patterns=["config.json", "tokenizer*", "vocab.txt",
                            "special_tokens_map.json", "model.safetensors"],
        )
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
        self.model = AutoModel.from_pretrained(snapshot, local_files_only=True).cpu().eval()
        self.dim = self.model.config.hidden_size
        self.model_version = MODEL + "@" + REVISION

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out = []
        for start in range(0, len(texts), 32):
            tokens = self.tokenizer(texts[start:start + 32], padding=True, truncation=True,
                                    max_length=512, return_tensors="pt")
            with self.torch.inference_mode():
                vectors = self.model(**tokens).last_hidden_state[:, 0]
                out.extend(self.torch.nn.functional.normalize(vectors, p=2, dim=1).tolist())
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([QUERY_PREFIX + text])[0]


class RemoteOpenAICompatibleProvider:
    """Disabled interface only. M1 implements no network client, keys or endpoint."""
    embedder = "remote-openai-compatible"

    def __init__(self, *, enabled: bool = False, endpoint: str | None = None,
                 api_key: str | None = None, model: str | None = None, dim: int = 512):
        if not enabled or not all((endpoint, api_key, model)):
            raise ValueError("remote embedding requires explicit authorization and configuration")
        if type(dim) is not int or dim < 1:
            raise ValueError("invalid embedding dimension")
        self.dim, self.model_version = dim, model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("M1 remote skeleton: no network implementation")

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError("M1 remote skeleton: no network implementation")
