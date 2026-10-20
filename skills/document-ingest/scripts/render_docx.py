#!/usr/bin/env python3
"""Stage A standalone preview: no database, ingestion or employee calls.

All artifacts are written beneath an explicitly supplied output directory.
The source is read-only; unsupported objects/quality issues return exit 2.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from docx_fidelity import atomic_bytes, project_docx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.source.resolve().is_relative_to(args.output_dir.resolve()):
        parser.error('output directory must not contain the original')
    digest = hashlib.sha256(args.source.read_bytes()).hexdigest()
    artifacts = project_docx(args.source, digest, args.output_dir / 'objects')
    atomic_bytes(args.output_dir / 'document.md', artifacts.pop('markdown').encode('utf-8'))
    for name, value in artifacts.items():
        atomic_bytes(args.output_dir / f'{name}.json', json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))
    print(json.dumps({'file_hash': digest, 'output_dir': str(args.output_dir),
                      'self_check': artifacts['self_check']}, ensure_ascii=False))
    return 0 if artifacts['self_check']['status'] == 'pass' else 2


if __name__ == '__main__':
    raise SystemExit(main())
