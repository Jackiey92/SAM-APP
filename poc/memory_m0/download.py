"""Timed first population of a dedicated, reproducible PoC cache."""
import json,time
from pathlib import Path
from huggingface_hub import snapshot_download
root=Path(__file__).resolve().parent
cache=root/'model-cache'
if cache.exists(): raise SystemExit('Refusing to label an existing cache as a first download')
t=time.perf_counter()
path=snapshot_download('BAAI/bge-small-zh-v1.5', cache_dir=str(cache),allow_patterns=['config.json','tokenizer*','vocab.txt','special_tokens_map.json','model.safetensors'])
elapsed=time.perf_counter()-t
size=sum(p.stat().st_size for p in Path(path).iterdir() if p.is_file())
(root/'download.json').write_text(json.dumps({'snapshot':path,'logical_snapshot_bytes':size,'first_dedicated_cache_download_seconds':elapsed},indent=2))
print(elapsed,size)
