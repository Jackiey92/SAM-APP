"""Pure complete-row differences for already assembled projection rows."""
from dataclasses import dataclass


class RowDeltaError(ValueError):
    """Invalid row input or duplicate stable key."""


@dataclass(frozen=True)
class RowChanges:
    added: tuple[tuple, ...]
    modified: tuple[tuple, ...]
    removed: tuple[tuple, ...]

    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)


def diff_rows(parent_rows, child_rows, key) -> RowChanges:
    """Compare complete tuples by unique nonempty string keys, without mutation."""
    if not callable(key):
        raise RowDeltaError("key must be callable")

    def index(rows):
        try:
            iterator = iter(rows)
        except TypeError as exc:
            raise RowDeltaError("rows must be iterable") from exc
        result = {}
        for row in iterator:
            if not isinstance(row, tuple):
                raise RowDeltaError("row must be a tuple")
            try:
                value = key(row)
            except (IndexError, TypeError, ValueError, KeyError) as exc:
                raise RowDeltaError("cannot extract row key: " + str(exc)) from exc
            if not isinstance(value, str) or not value:
                raise RowDeltaError("row key must be a nonempty string")
            if value in result:
                raise RowDeltaError("duplicate row key: " + value)
            result[value] = row
        return result

    parent, child = index(parent_rows), index(child_rows)
    return RowChanges(
        tuple(child[k] for k in sorted(child.keys() - parent.keys())),
        tuple(child[k] for k in sorted(parent.keys() & child.keys()) if parent[k] != child[k]),
        tuple(parent[k] for k in sorted(parent.keys() - child.keys())))


def wide_row_changes(parent_assembled, child_assembled) -> dict[str, RowChanges]:
    """Diff every family in sorted union order; path is the fourth-last column."""
    return {family: diff_rows(parent_assembled.get(family, []),
                              child_assembled.get(family, []), lambda row: row[-4])
            for family in sorted(parent_assembled.keys() | child_assembled.keys())}


def fts_row_changes(parent_rows, child_rows) -> RowChanges:
    """Detect content changes; a nonempty result requires later full FTS rebuild."""
    return diff_rows(parent_rows, child_rows, lambda row: row[0])
