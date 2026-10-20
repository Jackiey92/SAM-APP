"""Operational sources contain no retired transport identifiers/endpoints."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ('app', 'scripts', 'webapp', 'skills', 'schemas', 'profiles',
                'harness-openclaw/config', 'harness-openclaw/plugins',
                'harness-openclaw/skill-registry', 'harness-openclaw/tool-registry')

def test_runtime_has_no_retired_connector_or_endpoint():
    pattern = re.compile(r'viking|openviking|ovcli|1933', re.I)
    candidates = [ROOT / 'sam-manifest.yaml', *(ROOT / 'poc/memory_m0').glob('*.py')]
    for root in SOURCE_ROOTS:
        candidates.extend(p for p in (ROOT / root).rglob('*') if p.suffix in {'.py','.sh','.js','.json','.yaml','.md','.html','.css'})
    hits = [(str(p.relative_to(ROOT)), i, line) for p in candidates if p.is_file()
            for i, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert hits == []
    assert not list((ROOT / 'scripts').glob('*' + 'local-ov' + '*'))
