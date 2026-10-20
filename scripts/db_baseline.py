"""Read-only database/evidence baseline; never imports the application."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def snapshot(path):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    result = {'path': str(path), 'mtime_ns': before.st_mtime_ns, 'sha256': digest}
    if path.suffix in {'.db', '.sqlite', '.sqlite3'}:
        # immutable avoids even journal/shm creation. Refuse a live WAL rather
        # than reporting misleading main-file counts.
        wal = Path(str(path) + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError(f'live WAL: quiesce writer before snapshot: {path}')
        conn = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            result['rows'] = {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                              for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
        finally:
            conn.close()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise RuntimeError(f'changed during snapshot: {path}')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*')
    parser.add_argument('--evidence-root', type=Path, help='explicit original-material root; recursively hash files')
    args = parser.parse_args()
    paths = list(args.paths)
    if args.evidence_root is not None:
        root = args.evidence_root.resolve(strict=True)
        if not root.is_dir():
            parser.error('--evidence-root must be a directory')
        paths += [path for path in sorted(root.rglob('*')) if path.is_file()]
    if not paths:
        parser.error('supply database paths or --evidence-root')
    print(json.dumps([snapshot(path) for path in paths], ensure_ascii=False, indent=2))
