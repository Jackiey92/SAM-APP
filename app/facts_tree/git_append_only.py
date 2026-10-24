"""Pure append-only retention validator for facts Git history entries.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 section 10.2.2 (history revisions, evidence, operations,
consumed candidates and event files are append-only; current pointers
may advance in legal operations; old versions are never rewritten in
place or deleted) and section 14.2 (a tree whose old versions were
changed must not be accepted; the accepted base stays untouched), plus
the DEC-030 approved "97-业务记录与投影重建设计.md" section 3.2
(published taxonomy manifest/leaf records must not be rewritten in
place or deleted; the append-only module must be extended to protect
that family).

Public entry point:

    validate_append_only_retention(base_entries, new_entries)

Entry structure (fixed, one shape for both arguments)
-----------------------------------------------------
Each argument is an iterable of entries; each entry must be a tuple of
exactly two items ``(path, oid)``:

- ``path``: str, the repository-relative record path. Only its type is
  checked here; path safety, whitelist membership and tree shape belong
  to ``git_tree_validation``.
- ``oid``: str, the object id the caller recorded for that path. Only
  its type is checked; object existence, Git object type and OID format
  are the caller's and the object store's concern.

Append-only families
--------------------
A path belongs to an append-only family when
``git_format.canonical_record_type(path)`` returns one of the record
types P01 section 10.2.2 explicitly describes as append-only:

    entity_revision, fact_revision, source_version, source_lifecycle,
    evidence, link_revision, placement_revision, issue_event,
    proposal_event, input_candidate, operation, consumed_candidate,
    event

plus the two taxonomy record types that 97 section 3.2 protects the
same way once published:

    taxonomy_manifest, taxonomy_leaf

The classification is delegated to ``canonical_record_type`` so this
module never re-implements the fixed path templates in parallel; a
path that merely looks like a template but is not the canonical
encoding (for example an escape alias such as "~2D" for "-") is not a
member of any record type and gets no retention guarantee from this
function. ``repository_meta`` is not in the set either: it is not one
of the record types section 10.2.2 lists as append-only, and the
repository identity it carries is already guaranteed immutable by the
accept() gate, which refuses any commit whose identity record differs
from the one bound at initialize()/open() time.
For the taxonomy family this validator protects every canonical
versioned path already present in the base entries as a written
versioned record: deleting or rewriting it is rejected, while adding a
new ``pack_version`` manifest or a new leaf version path stays legal.
Whether a pack version counts as formally published is a separate
gate's question; this function does not track publish state.
The ``*_current`` and ``bindings_current`` pointers are mutable by
design and are also outside the set, as is any path outside the fixed
layout.

Retention rule
--------------
For every append-only path present in ``base_entries``: if the path is
absent from ``new_entries`` the old record was deleted, and if it is
present with a different oid the old record was rewritten in place;
both raise ValueError. Paths that exist only in ``new_entries`` are
new revisions or events and are legal. Paths outside the append-only
families may appear, disappear or change oid freely.

Return and error contract
-------------------------
Returns None on success and never mutates its inputs. TypeError
signals wrong types (a bare str/bytes or non-iterable passed where an
iterable of entries is expected, a non-tuple entry, or a non-str
path/oid); ValueError signals wrong values (an entry tuple of the
wrong length, a duplicated path within either argument, or a deleted
or rewritten append-only record). Structural problems are reported in
input order; retention violations are reported in sorted path order,
so the first reported violation is independent of input order.

Boundary (explicitly NOT done here):

- This is a pure function over caller-supplied entries. It never walks
  a physical Git tree, queries an object store, reads or writes files,
  touches a ref or accesses SQLite; a well-formed comparison is not
  proof that any listed object exists. Walking the physical trees and
  calling this validator before the authoritative ref CAS is the
  accept() gate's job in ``git_repository``.
- It does not validate OID shape, business record schema, identity or
  authorization; the final Commit Broker hookup is a separate task.
"""
from app.facts_tree import git_format

APPEND_ONLY_RECORD_TYPES = (
    "consumed_candidate",
    "entity_revision",
    "event",
    "evidence",
    "fact_revision",
    "input_candidate",
    "issue_event",
    "link_revision",
    "operation",
    "placement_revision",
    "proposal_event",
    "source_lifecycle",
    "source_version",
    "taxonomy_leaf",
    "taxonomy_manifest",
)

_APPEND_ONLY_TYPES = frozenset(APPEND_ONLY_RECORD_TYPES)


def validate_append_only_retention(base_entries, new_entries):
    """Check that no append-only base record was deleted or rewritten.

    See the module docstring for the entry structure ``(path, oid)``,
    the append-only family set and the full return/error contract.
    Returns None on success.
    """
    base = _index("base_entries", base_entries)
    new = _index("new_entries", new_entries)
    for path in sorted(base):
        if git_format.canonical_record_type(path) not in _APPEND_ONLY_TYPES:
            continue
        if path not in new:
            raise ValueError(
                "append-only record %r was deleted in the new entries"
                % path)
        if new[path] != base[path]:
            raise ValueError(
                "append-only record %r was rewritten in place: old "
                "versions must be retained, not replaced" % path)
    return None


def _index(name, entries):
    if isinstance(entries, (str, bytes)):
        raise TypeError(
            "%s must be an iterable of (path, oid) tuples, not a "
            "single %s" % (name, type(entries).__name__))
    try:
        items = list(entries)
    except TypeError:
        raise TypeError(
            "%s must be an iterable of (path, oid) tuples" % name) from None
    indexed = {}
    for entry in items:
        if not isinstance(entry, tuple):
            raise TypeError(
                "%s entry must be a tuple (path, oid), got %s"
                % (name, type(entry).__name__))
        if len(entry) != 2:
            raise ValueError(
                "%s entry must have exactly 2 items (path, oid), got %d"
                % (name, len(entry)))
        path, oid = entry
        if not isinstance(path, str):
            raise TypeError(
                "%s entry path must be a str, got %s"
                % (name, type(path).__name__))
        if not isinstance(oid, str):
            raise TypeError(
                "%s entry %r oid must be a str, got %s"
                % (name, path, type(oid).__name__))
        if path in indexed:
            raise ValueError("%s path %r is duplicated" % (name, path))
        indexed[path] = oid
    return indexed
