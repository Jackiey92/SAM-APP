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


class EmbeddingRequestError(ValueError):
    """Remote request failed or returned an invalid embedding response."""


class RemoteOpenAICompatibleProvider:
    """Explicitly authorized OpenAI-compatible embedding client, without fallback."""
    embedder = "remote-openai-compatible"

    def __init__(self, *, enabled: bool = False, endpoint: str | None = None,
                 api_key: str | None = None, model: str | None = None,
                 dim: int = 512, timeout: float = 30.0,
                 embedder: str = "remote-openai-compatible"):
        if not enabled or not all((endpoint, api_key, model)):
            raise ValueError("remote embedding requires explicit authorization and configuration")
        if type(dim) is not int or dim < 1:
            raise ValueError("invalid embedding dimension")
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("invalid embedding timeout")
        self.enabled = True
        self.endpoint = endpoint.rstrip("/")
        self.api_key, self.model_version, self.dim = api_key, model, dim
        self.timeout, self.embedder = float(timeout), embedder

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        import http.client
        import json
        import math
        import urllib.error
        import urllib.request

        if not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("embedding input must be a list of nonempty strings")
        if not texts:
            return []
        try:
            request = urllib.request.Request(
                self.endpoint + "/embeddings",
                data=json.dumps({"model": self.model_version, "input": texts,
                                 "dimensions": self.dim, "encoding_format": "float"}).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            rows = data["data"]
            if not isinstance(rows, list) or len(rows) != len(texts):
                raise ValueError
            if any(not isinstance(row, dict) or type(row["index"]) is not int for row in rows):
                raise ValueError
            if {row["index"] for row in rows} != set(range(len(texts))):
                raise ValueError
            result = []
            for row in sorted(rows, key=lambda row: row["index"]):
                vector = row["embedding"]
                if not isinstance(vector, list) or len(vector) != self.dim:
                    raise ValueError
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector) or not any(vector):
                    raise ValueError
                result.append(vector)
            return result
        except (urllib.error.URLError, OSError, http.client.HTTPException,
                KeyError, IndexError, TypeError, UnicodeError, ValueError, OverflowError):
            raise EmbeddingRequestError("embedding request failed or returned invalid data") from None

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]
