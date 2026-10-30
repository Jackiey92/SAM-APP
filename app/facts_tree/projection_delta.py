"""Pure byte-level differences and replay for adjacent record snapshots."""
from dataclasses import dataclass

from app.facts_tree.git_repository import SnapshotRecord


class ProjectionDeltaError(ValueError):
    """Invalid snapshot input or inconsistent delta application."""


@dataclass(frozen=True)
class RecordChange:
    path: str
    before: bytes | None
    after: bytes | None


@dataclass(frozen=True)
class TreeDelta:
    added: tuple[RecordChange, ...]
    modified: tuple[RecordChange, ...]
    removed: tuple[RecordChange, ...]

    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)


def _valid_path(path):
    if not isinstance(path, str) or not path:
        raise ProjectionDeltaError("record path must be a nonempty str")


def _normalize(records):
    try:
        iterator = iter(records)
    except TypeError as exc:
        raise ProjectionDeltaError("records must be iterable") from exc
    contents = {}
    for record in iterator:
        if isinstance(record, SnapshotRecord):
            path, content = record.path, record.content
        elif isinstance(record, tuple) and len(record) == 2:
            path, content = record
        else:
            raise ProjectionDeltaError("record must be SnapshotRecord or (str, bytes)")
        _valid_path(path)
        if not isinstance(content, bytes):
            raise ProjectionDeltaError("record content must be bytes")
        if path in contents:
            raise ProjectionDeltaError("duplicate record path: %s" % path)
        contents[path] = content
    return contents


def tree_delta(parent_records, child_records) -> TreeDelta:
    """Compare raw bytes, leaving record interpretation to the caller."""
    parent, child = _normalize(parent_records), _normalize(child_records)
    added, modified, removed = [], [], []
    for path in sorted(parent.keys() | child.keys()):
        if path not in parent:
            added.append(RecordChange(path, None, child[path]))
        elif path not in child:
            removed.append(RecordChange(path, parent[path], None))
        elif parent[path] != child[path]:
            modified.append(RecordChange(path, parent[path], child[path]))
    return TreeDelta(tuple(added), tuple(modified), tuple(removed))


def apply_tree_delta(base_records, delta: TreeDelta) -> tuple[SnapshotRecord, ...]:
    """Validate every change against base and return a fresh sorted snapshot."""
    base = _normalize(base_records)
    if not isinstance(delta, TreeDelta):
        raise ProjectionDeltaError("delta must be TreeDelta")
    seen = set()
    for kind in ("added", "modified", "removed"):
        changes = getattr(delta, kind)
        if not isinstance(changes, tuple):
            raise ProjectionDeltaError("delta groups must be tuples")
        previous = None
        for change in changes:
            if not isinstance(change, RecordChange):
                raise ProjectionDeltaError("delta entries must be RecordChange")
            path, before, after = change.path, change.before, change.after
            _valid_path(path)
            if path in seen:
                raise ProjectionDeltaError("duplicate change path: %s" % path)
            if previous is not None and path <= previous:
                raise ProjectionDeltaError("delta groups must be sorted by path")
            seen.add(path)
            previous = path
            if kind == "added":
                valid = before is None and isinstance(after, bytes) and path not in base
            elif kind == "modified":
                valid = (isinstance(before, bytes) and isinstance(after, bytes)
                         and before != after and path in base and base[path] == before)
            else:
                valid = (isinstance(before, bytes) and after is None
                         and path in base and base[path] == before)
            if not valid:
                raise ProjectionDeltaError("inconsistent %s change: %s" % (kind, path))
    result = base.copy()
    for change in delta.added + delta.modified:
        result[change.path] = change.after
    for change in delta.removed:
        del result[change.path]
    return tuple(SnapshotRecord(path, result[path]) for path in sorted(result))
