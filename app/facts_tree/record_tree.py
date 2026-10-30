"""Read-only assembly of a fixed Git snapshot (appendix C v1.2).

The core parses canonical bytes and validates schemas and closures 1-5 on a
native dict before exposing read-only mapping views. Payloads keep their JSON
dict/list representation for lossless serialization and public revalidation.
Only the repository wrapper performs IO; storage errors pass through unchanged.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.facts_tree import bindings_current, git_format
from app.facts_tree.git_repository import SnapshotRecord
from app.facts_tree.record_schema import (
    loads_schema_record, validate_record_schema_closure,
)


@dataclass(frozen=True)
class ValidatedRecordTree:
    commit_oid: str
    head_oid: str
    ordered: Mapping[str, Mapping]
    index: Mapping[tuple, str]
    families: Mapping[str, tuple[str, ...]]
    record_count: int
    repo_id: str
    company_id: str
    object_format: str


def build_record_tree(records, *, repo_id, company_id, commit_oid, head_oid,
                      object_format="sha1") -> ValidatedRecordTree:
    """Validate canonical (path, bytes) pairs or SnapshotRecords without IO.

    Schema/closure exceptions pass through unchanged. Malformed assembly input
    raises built-in TypeError/ValueError, with no partial result returned.
    """
    contents = {}
    for record in records:
        if isinstance(record, SnapshotRecord):
            path, content = record.path, record.content
        else:
            path, content = record
        if type(path) is not str:
            raise TypeError("record path must be a str")
        if not isinstance(content, bytes):
            raise TypeError("record content must be bytes")
        if path in contents:
            raise ValueError("duplicate record path: %s" % path)
        contents[path] = content

    ordered = {
        path: loads_schema_record(
            contents[path], path=path, repo_id=repo_id, company_id=company_id,
            object_format=object_format)
        for path in sorted(contents)
    }
    # The public closure entry requires exactly a native dict. Freeze views
    # only after it has validated the complete tree, including empty trees.
    validate_record_schema_closure(
        ordered, repo_id=repo_id, company_id=company_id,
        object_format=object_format)

    index, families = {}, {}
    for path, payload in ordered.items():
        family = git_format.canonical_record_type(path)
        families.setdefault(family, []).append(path)
        if family in {"entity_current", "fact_current", "placement_current",
                      "bindings_current"}:
            continue
        identities = tuple(git_format._match_template(family, path).values())
        if identities:
            key = (family, identities[0],
                   identities[1] if len(identities) == 2 else None)
            index[key] = path
        if (family == "event"
                and payload["event_type"] in bindings_current.BINDING_EVENT_TYPES):
            # Closure has already rejected duplicate logical binding targets.
            index[("bindings_current", bindings_current.CURRENT_STABLE_ID,
                   payload["after"]["binding_revision"])] = path

    return ValidatedRecordTree(
        commit_oid=commit_oid, head_oid=head_oid,
        ordered=MappingProxyType(ordered), index=MappingProxyType(index),
        families=MappingProxyType({key: tuple(paths)
                                   for key, paths in sorted(families.items())}),
        record_count=len(ordered), repo_id=repo_id, company_id=company_id,
        object_format=object_format)


def read_validated_record_tree(repository, commit_oid, *, object_format=None
                               ) -> ValidatedRecordTree:
    """Read fixed C once, using only the identity bound to the repository."""
    snapshot = repository.read_snapshot_at(commit_oid)
    identity = repository._expected_meta
    return build_record_tree(
        snapshot.records, repo_id=identity["repo_id"],
        company_id=identity["company_id"], commit_oid=snapshot.commit_oid,
        head_oid=snapshot.head_oid,
        object_format=(repository.object_format if object_format is None
                       else object_format))
