"""Fixed, host-owned original page rendering in the same offline enclosure."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .sandbox import QualitySandbox, SandboxLimits, SandboxFailure
from ..storage.mapping_store import write_mapping


_RENDER_SCRIPT = '''
from pathlib import Path
import base64, json, subprocess, os
import pymupdf
source = next(Path('/evidence').glob('original.*'))
config = Path('/work/fonts.conf')
config.write_text('<fontconfig><dir>/opt/sam/fonts</dir><dir>/usr/share/fonts</dir><cachedir>/work/font-cache</cachedir></fontconfig>')
os.environ['FONTCONFIG_FILE'] = str(config)
if source.suffix.lower() != '.pdf':
    converted = subprocess.run(['/usr/bin/soffice', '-env:UserInstallation=file:///work/lo',
        '--headless', '--convert-to', 'pdf', '--outdir', '/work', str(source)],
        capture_output=True, timeout=70)
    if converted.returncode:
        raise RuntimeError('original page conversion failed: ' + converted.stderr.decode(errors='replace')[:2000])
    source = Path('/work') / (source.stem + '.pdf')
with pymupdf.open(source) as document:
    pages = []
    for i in range(min(len(document), 64)):
        png = document[i].get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False).tobytes('png')
        pages.append({'page': i + 1, 'png': base64.b64encode(png).decode('ascii')})
    print(json.dumps({'page_count': len(document), 'pages': pages,
        'warnings': ['Only first 64 pages rendered'] if len(document) > 64 else []}))
'''


def render_original_pages(*, sandbox: QualitySandbox, source: Path, file_hash: str,
                          company_id: str, task_id: str, maps_path: Path, file_format: str | None = None) -> dict:
    import hashlib
    font_directory = sandbox.venv.parent / 'fonts'
    font_signature = hashlib.sha256(''.join(
        str(p.name) + str(p.stat().st_size) + str(p.stat().st_mtime_ns)
        for p in sorted(font_directory.glob('*')) if p.is_file()).encode()).hexdigest()
    directory = maps_path / file_hash[:2] / (file_hash + '.pages')
    manifest_path = directory / 'index.json'
    if manifest_path.is_file():
        cached = json.loads(manifest_path.read_text())
        if cached.get('file_hash') == file_hash and cached.get('font_signature') == font_signature and cached.get('renderer_version') == 2 and cached.get('pages') and all((directory / item['filename']).is_file() for item in cached.get('pages', [])):
            return cached
    renderer = QualitySandbox(sandbox.venv, temporary_root=sandbox.temporary_root,
        limits=SandboxLimits(wall_seconds=90, cpu_seconds=60, disk_bytes=128 * 1024 * 1024,
                             memory_bytes=2 * 1024 ** 3, output_bytes=64 * 1024 * 1024))
    try:
        raw = renderer._execute(company_id=company_id, task_id=task_id,
            script=_RENDER_SCRIPT, evidence={'original.' + (file_format or source.suffix.lstrip('.').lower()): source.read_bytes()})
        rendered = json.loads(raw)
        # Atomic bytes helper intentionally local: page PNG is not a text map.
        from tempfile import NamedTemporaryFile
        import os
        directory.mkdir(parents=True, exist_ok=True)
        pages = []
        for page in rendered['pages']:
            name = f"page-{page['page']:03d}.png"
            temporary = None
            try:
                with NamedTemporaryFile(dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(base64.b64decode(page['png'], validate=True))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, directory / name)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            pages.append({'page': page['page'], 'filename': name})
        result = {'file_hash': file_hash, 'renderer': 'local-office-pdf/216dpi',
                  'renderer_version': 2, 'font_signature': font_signature,
                  'page_count': rendered['page_count'], 'pages': pages, 'warnings': rendered['warnings']}
    except (SandboxFailure, OSError, ValueError, KeyError) as exc:
        result = {'file_hash': file_hash, 'pages': [], 'warnings': [f'原件页面快照不可用：{exc}']}
    write_mapping(manifest_path, json.dumps(result, ensure_ascii=False, indent=2))
    return result
