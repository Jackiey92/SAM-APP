"""Explicit, path-required SAM schema migration."""
import argparse
from pathlib import Path
from .database import init_db, validate_schema, SCHEMA_VERSION


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--snapshot', action='store_true', help='migrate the separate cloud snapshot store')
    args = parser.parse_args(argv)
    if args.db.is_file():
        try:
            validate_schema(args.db, snapshot=args.snapshot)
        except RuntimeError:
            pass  # missing/older schema is handled by the explicit migration
        else:
            print(f"schema {SCHEMA_VERSION} already current: {args.db}")
            return
    if args.snapshot:
        from ..cloud_snapshot import initialize_snapshot
        initialize_snapshot(args.db)
        print(f'migrated snapshot {args.db}')
        return
    from types import SimpleNamespace
    from ..memory.embedding import MODEL, REVISION
    init_db(args.db, embedding=SimpleNamespace(embedder="local-bge", model_version=MODEL + "@" + REVISION, dim=512))
    print(f"migrated {args.db} to schema {SCHEMA_VERSION}")


if __name__ == '__main__':
    main()
