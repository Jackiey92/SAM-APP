import os
from typing import Protocol

class EmbeddingProvider(Protocol):
    model_version: str
    dim: int
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...

class LocalBgeProvider:
    def __init__(self, model='BAAI/bge-small-zh-v1.5'):
        import torch
        from transformers import AutoTokenizer, AutoModel
        self.torch = torch
        torch.set_num_threads(4)
        offline = os.environ.get('HF_HUB_OFFLINE') == '1'
        from huggingface_hub import snapshot_download
        from pathlib import Path
        cache = Path(__file__).resolve().parent / 'model-cache'
        snapshot = snapshot_download(model, cache_dir=str(cache), revision='7999e1d3359715c523056ef9478215996d62a620', local_files_only=offline, allow_patterns=['config.json', 'tokenizer*', 'vocab.txt', 'special_tokens_map.json', 'model.safetensors'])
        self.tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
        self.model = AutoModel.from_pretrained(snapshot, local_files_only=True).cpu().eval()
        self.dim = self.model.config.hidden_size
        self.model_version = model + '@' + __import__('pathlib').Path(snapshot).name
    def embed_texts(self, texts):
        out = []
        for start in range(0, len(texts), 32):
            tokens = self.tokenizer(texts[start:start+32], padding=True, truncation=True, max_length=512, return_tensors='pt')
            with self.torch.inference_mode():
                vectors = self.model(**tokens).last_hidden_state[:, 0]
                out.extend(self.torch.nn.functional.normalize(vectors, p=2, dim=1).tolist())
        return out
    def embed_query(self, text):
        return self.embed_texts(['为这个句子生成表示以用于检索相关文章：' + text])[0]

class RemoteOpenAICompatibleProvider:
    def __init__(self, endpoint, api_key, model):
        if os.environ.get('SAM_EMBEDDING_PROVIDER') != 'remote':
            raise ValueError('remote provider requires explicit opt-in')
        if not all((endpoint, api_key, model)):
            raise ValueError('remote configuration incomplete')
        self.endpoint, self.api_key, self.model_version = endpoint, api_key, model
        self.dim = 512
    def embed_texts(self, texts):
        raise NotImplementedError('M0 remote skeleton: no network implementation')
    def embed_query(self, text):
        return self.embed_texts([text])[0]
