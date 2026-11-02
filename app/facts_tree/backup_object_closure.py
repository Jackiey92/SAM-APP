"""Read-only R necessary-immutable-object closure for F-001 backups.

Implements appendix R1 of the approved F-001 backup design
(``98-完整备份与隔离恢复设计.md`` §2.3/§2.4): given the fixed H commit
oid (pinned by a different slice — this module never pins), the
authorized installation/company scope, and a caller-extracted set of
logical object references from H history and W checkpoints, collect the
necessary immutable object closure and emit a retention-pin
*description* plus a stable R-list digest. This is a pure library
collector: it holds no maintenance barrier, takes no lock, creates no
GC-protection pin, copies no bytes onto backup media, and does no
assembly, restore or GC — those belong to later slices.

What the closure covers (design §2.3): originals, exact historical
mapping-version bytes (an old v1 is collected by its own digest and can
never be satisfied by the newest v2), attachments, complete sensitive
candidate envelopes, and necessary externalized audit/send evidence.
Every object records logical id, type, sha256, byte size, scope,
immutable version, every reference root that cites it (H commit record
or W checkpoint coordinate, both traceable), and the encryption key
reference (explicit ``None`` when the object is not encrypted — never
invented).

Byte verification never reimplements hashing or path traversal: each
present object is verified through
:func:`app.facts_tree.backup_component_validation.verify_backup_component`
against a descriptor opened on the content-addressed store root with
``O_DIRECTORY | O_NOFOLLOW``; the store-relative coordinate is derived
from the validated content digest, so a content-addressed object can
never name ``..`` or an absolute path, and an explicitly supplied
coordinate is lexically refused before any open. Symlinks,
directories, FIFOs and other non-regular occupants therefore fail
inside the pinned verifier and are reported, never read through.

Status semantics (fail closed):

- ``present`` — located and byte/size/sha256 verified.
- ``missing`` — referenced and expected externalized, but absent from
  its content-addressed store; a same-named file with different bytes
  is ``corrupt``, never a substitute.
- ``referenced_but_not_externalized`` — the reference still lives only
  inside W (SQLite); it is a gap, never a crash, and W inline bytes are
  never promoted into R by this module.
- ``corrupt`` — the stored bytes disagree with the registered digest
  or size; any corrupt object makes the whole closure unpublishable,
  with the object and every reference root citing it listed.
- ``unsafe_path`` — traversal/absolute/non-regular/symlink coordinate;
  no out-of-root byte is read.
- ``out_of_scope`` — objects belonging to another installation/company
  are refused from the reference set alone, before any object byte is
  read, and are never admitted into the closure (design §2.4).

Determinism: entries are ordered by ``(object_type, logical_id,
sha256)`` and reference roots are sorted, so repeated runs over the
same reference set produce byte-identical normalized lists and R
digest regardless of time, temp path or input ordering. The R digest
uses the exact manifest rule
(:func:`app.facts_tree.backup_manifest.compute_core_digest`, canonical
record serialization plus SHA-256) over a payload that contains no
self digest, no timestamp and no absolute path.

Scope guards: this module only reads. It never writes an object store,
moves/deletes/GCs an object, opens Git, creates a pin or checkpoint,
touches the network or a model, and never mutates a database (store
metadata is queried through read-only SELECTs). The returned
``retention_pin_request`` is a data description only; actually pinning
and copying happen inside the later maintenance-barrier assembly.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from app.storage.archive_store import ArchiveFileStore
from app.storage.versioned_mapping_objects import VersionedMappingObjectStore

from .backup_component_validation import (
    BackupComponentValidationError,
    verify_backup_component,
)
from .backup_manifest import compute_core_digest

DIGEST_ALGORITHM = "sha256"
CLOSURE_SCHEMA = "sam.f001.r_object_closure"
CLOSURE_VERSION = 1

# Object classes named by design §2.3.
OBJECT_ORIGINAL = "original"
OBJECT_MAPPING_VERSION = "mapping_version"
OBJECT_ATTACHMENT = "attachment"
OBJECT_CANDIDATE_ENVELOPE = "candidate_envelope"
OBJECT_AUDIT_EVIDENCE = "audit_evidence"
OBJECT_SEND_EVIDENCE = "send_evidence"
OBJECT_TYPES = frozenset((
    OBJECT_ORIGINAL,
    OBJECT_MAPPING_VERSION,
    OBJECT_ATTACHMENT,
    OBJECT_CANDIDATE_ENVELOPE,
    OBJECT_AUDIT_EVIDENCE,
    OBJECT_SEND_EVIDENCE,
))

# Which content-addressed store must hold the externalized bytes.
STORAGE_ARCHIVE = "archive"
STORAGE_MAPPING_OBJECTS = "mapping_objects"
_STORAGE_CLASSES = frozenset((STORAGE_ARCHIVE, STORAGE_MAPPING_OBJECTS))

# Reference-root families: H = pinned accepted history, W = checkpointed
# working/control state.
ROOT_H = "H"
ROOT_W = "W"
_ROOT_KINDS = frozenset((ROOT_H, ROOT_W))

SCOPE_INSTALLATION = "installation"
SCOPE_COMPANY = "company"
_SCOPE_KINDS = frozenset((SCOPE_INSTALLATION, SCOPE_COMPANY))

STATUS_PRESENT = "present"
STATUS_MISSING = "missing"
STATUS_NOT_EXTERNALIZED = "referenced_but_not_externalized"
STATUS_CORRUPT = "corrupt"
STATUS_UNSAFE_PATH = "unsafe_path"
STATUS_OUT_OF_SCOPE = "out_of_scope"
_GAP_STATUSES = frozenset((STATUS_MISSING, STATUS_NOT_EXTERNALIZED))

CLOSURE_COMPLETE = "complete"
CLOSURE_INCOMPLETE = "incomplete"
CLOSURE_CORRUPT = "corrupt"
CLOSURE_UNSAFE_PATHS = "unsafe_paths"
CLOSURE_SCOPE_VIOLATION = "scope_violation"

_DIGEST_HEX_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_COMMIT_OID_PATTERNS = {
    "sha1": re.compile(r"\A[0-9a-f]{40}\Z"),
    "sha256": re.compile(r"\A[0-9a-f]{64}\Z"),
}

_ROOT_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class ObjectClosureError(ValueError):
    """The closure call itself is malformed (bad API input), not a gap.

    Missing, corrupted, non-externalized, unsafe or out-of-scope
    *objects* are structured results, not this error; this is reserved
    for collector inputs that cannot describe a closure at all (wrong
    types, contradictory declarations, unusable store roots, missing
    verification capabilities).
    """


def _require_str(value, label, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ObjectClosureError(f"{label} must be a built-in str")
    if nonempty and not value:
        raise ObjectClosureError(f"{label} must not be empty")
    return value


def _require_object_format(object_format: str) -> str:
    if (not isinstance(object_format, str)
            or object_format not in _COMMIT_OID_PATTERNS):
        raise ObjectClosureError("object_format must be 'sha1' or 'sha256'")
    return object_format


def _require_commit_oid(value: str, label: str, object_format: str) -> None:
    _require_object_format(object_format)
    if not _COMMIT_OID_PATTERNS[object_format].fullmatch(value):
        length = 40 if object_format == "sha1" else 64
        raise ObjectClosureError(
            f"{label} must be the {length}-hex fixed commit oid"
        )


def _require_digest(value, label) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ObjectClosureError(
            f"{label} must be 'sha256:' plus 64 lowercase hex characters"
        )
    return value


def _clean_relative_coordinate(value: str, label: str) -> str:
    """Lexical shape check only; it opens nothing and walks nothing.

    It mirrors the relative-path contract that
    ``verify_backup_component`` enforces at read time, so an explicitly
    supplied store coordinate is refused here as ``unsafe_path``
    *before* any descriptor walk. Digest-derived coordinates are always
    clean by construction and never pass through an untrusted string.
    """
    if not isinstance(value, str) or not value:
        raise ObjectClosureError(f"{label} must be a non-empty str")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        return ""  # caller treats empty as an unsafe traversal coordinate
    for segment in value.split("/"):
        if segment == "" or segment in (".", ".."):
            return ""
    return value


@dataclass(frozen=True)
class ReferenceRoot:
    """One provenance root citing an object: an H record or a W row.

    ``root_id`` is the fixed commit oid for H roots or the checkpoint
    identifier for W roots; ``coordinate`` is the exact record path or
    table/row locator that carries the citation.
    """

    kind: str
    root_id: str
    coordinate: str
    object_format: str = field(default="sha1", kw_only=True)

    def __post_init__(self) -> None:
        if self.kind not in _ROOT_KINDS:
            raise ObjectClosureError(
                "reference root kind must be 'H' or 'W', got "
                f"{self.kind!r}"
            )
        _require_str(self.root_id, "reference root id")
        _require_str(self.coordinate, "reference root coordinate")
        _require_object_format(self.object_format)
        if self.kind == ROOT_H:
            _require_commit_oid(
                self.root_id, "H reference root id", self.object_format
            )

    def normalized(self) -> dict[str, str]:
        return {"kind": self.kind, "root_id": self.root_id,
                "coordinate": self.coordinate}


@dataclass(frozen=True)
class ObjectScope:
    """The scope an object belongs to: installation-wide or one company."""

    kind: str
    installation_id: str
    company_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _SCOPE_KINDS:
            raise ObjectClosureError(
                "scope kind must be 'installation' or 'company'"
            )
        _require_str(self.installation_id, "scope installation_id")
        if self.kind == SCOPE_COMPANY:
            _require_str(self.company_id, "company scope company_id")
        elif self.company_id is not None:
            raise ObjectClosureError(
                "installation scope must not carry a company_id"
            )

    def normalized(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "installation_id": self.installation_id,
            "company_id": self.company_id,
        }


@dataclass(frozen=True)
class ClosureScope:
    """The single authorized installation/company for this collection."""

    installation_id: str
    company_id: str

    def __post_init__(self) -> None:
        _require_str(self.installation_id, "authorized installation_id")
        _require_str(self.company_id, "authorized company_id")

    def normalized(self) -> dict[str, str]:
        return {
            "installation_id": self.installation_id,
            "company_id": self.company_id,
        }

    def admits(self, scope: ObjectScope) -> str | None:
        """Return a stable violation reason, or ``None`` when in scope."""
        if scope.installation_id != self.installation_id:
            return "foreign_installation"
        if scope.kind == SCOPE_INSTALLATION:
            return None
        if scope.company_id != self.company_id:
            return "foreign_company"
        return None


@dataclass(frozen=True)
class ObjectReference:
    """One caller-extracted logical object citation from H or W.

    A reference with ``sha256_digest=None`` declares an object that is
    still only held inside W (SQLite): it is reported as
    ``referenced_but_not_externalized`` and is never substituted with
    inline bytes. A present digest selects the content-addressed store
    via ``storage_class`` (mapping versions live in the immutable
    mapping-object store, everything else in the archive object store)
    and the store-relative coordinate is derived from the digest;
    ``relative_path`` is an optional explicit archive coordinate for
    objects whose layout name is not the digest shard, and it is
    lexically confined to the root.
    """

    logical_id: str
    object_type: str
    version: str
    referenced_by: tuple[ReferenceRoot, ...] | list[ReferenceRoot]
    scope: ObjectScope
    sha256_digest: str | None = None
    key_reference: str | None = None
    storage_class: str | None = None
    relative_path: str | None = None

    def __post_init__(self) -> None:
        _require_str(self.logical_id, "reference logical_id")
        if self.object_type not in OBJECT_TYPES:
            raise ObjectClosureError(
                f"unknown object_type {self.object_type!r}"
            )
        _require_str(self.version, "reference immutable version")
        if not isinstance(self.scope, ObjectScope):
            raise ObjectClosureError("reference scope must be ObjectScope")
        if not isinstance(self.referenced_by, (list, tuple)) \
                or not self.referenced_by:
            raise ObjectClosureError(
                "reference must name at least one reference root"
            )
        roots = tuple(self.referenced_by)
        if any(not isinstance(root, ReferenceRoot) for root in roots):
            raise ObjectClosureError(
                "reference roots must be ReferenceRoot instances"
            )
        # Canonicalize without letting two identical roots cite twice.
        deduped = tuple(sorted(set(roots),
                               key=lambda r: (r.kind, r.root_id,
                                              r.coordinate)))
        object.__setattr__(self, "referenced_by", deduped)
        if self.key_reference is not None:
            _require_str(self.key_reference, "key_reference")

        if self.relative_path is not None and not isinstance(
                self.relative_path, str):
            raise ObjectClosureError("relative_path must be a str or None")

        if self.sha256_digest is None:
            if self.storage_class is not None:
                raise ObjectClosureError(
                    "a not-yet-externalized reference cannot name a store"
                )
            if self.relative_path is not None:
                raise ObjectClosureError(
                    "a not-yet-externalized reference cannot name a path"
                )
            return

        _require_digest(self.sha256_digest, "reference sha256_digest")
        default_class = (
            STORAGE_MAPPING_OBJECTS
            if self.object_type == OBJECT_MAPPING_VERSION
            else STORAGE_ARCHIVE
        )
        storage_class = self.storage_class or default_class
        if storage_class not in _STORAGE_CLASSES:
            raise ObjectClosureError(
                f"unknown storage_class {storage_class!r}"
            )
        if self.object_type == OBJECT_MAPPING_VERSION:
            if storage_class != STORAGE_MAPPING_OBJECTS:
                raise ObjectClosureError(
                    "mapping versions are externalized only in the "
                    "mapping-object store"
                )
            if self.relative_path is not None:
                raise ObjectClosureError(
                    "mapping coordinates are digest-derived; an explicit "
                    "relative_path is not accepted"
                )
        elif storage_class != STORAGE_ARCHIVE:
            raise ObjectClosureError(
                f"{self.object_type} objects are externalized in the "
                "archive store"
            )
        object.__setattr__(self, "storage_class", storage_class)

    @property
    def digest_hex(self) -> str | None:
        if self.sha256_digest is None:
            return None
        return self.sha256_digest[len(DIGEST_ALGORITHM) + 1:]


@dataclass(frozen=True)
class ClosureStores:
    """The two real content-addressed stores the closure reads from."""

    archive: ArchiveFileStore
    mapping_objects: VersionedMappingObjectStore

    def __post_init__(self) -> None:
        if not isinstance(self.archive, ArchiveFileStore):
            raise ObjectClosureError(
                "stores.archive must be a real ArchiveFileStore"
            )
        if not isinstance(self.mapping_objects,
                          VersionedMappingObjectStore):
            raise ObjectClosureError(
                "stores.mapping_objects must be a real "
                "VersionedMappingObjectStore"
            )


@dataclass(frozen=True)
class ClosureObjectEntry:
    """One logical object in the closure with its verification verdict."""

    logical_id: str
    object_type: str
    version: str
    referenced_by: tuple[ReferenceRoot, ...]
    scope: ObjectScope
    status: str
    sha256_digest: str | None = None
    size_bytes: int | None = None
    key_reference: str | None = None
    storage_class: str | None = None
    relative_path: str | None = None
    issue: str | None = None

    def normalized(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "object_type": self.object_type,
            "digest_algorithm": (
                DIGEST_ALGORITHM if self.sha256_digest is not None else None
            ),
            "sha256_digest": self.sha256_digest,
            "size_bytes": self.size_bytes,
            "scope": self.scope.normalized(),
            "version": self.version,
            "key_reference": self.key_reference,
            "storage_class": self.storage_class,
            "relative_path": self.relative_path,
            "status": self.status,
            "issue": self.issue,
            "referenced_by": [root.normalized()
                              for root in self.referenced_by],
        }


@dataclass(frozen=True)
class ScopeViolation:
    """A cited object that belongs outside the authorized scope."""

    logical_id: str
    object_type: str
    version: str
    referenced_by: tuple[ReferenceRoot, ...]
    scope: ObjectScope
    reason: str
    sha256_digest: str | None = None
    key_reference: str | None = None

    def normalized(self) -> dict[str, Any]:
        return {
            "logical_id": self.logical_id,
            "object_type": self.object_type,
            "sha256_digest": self.sha256_digest,
            "version": self.version,
            "key_reference": self.key_reference,
            "scope": self.scope.normalized(),
            "reason": self.reason,
            "referenced_by": [root.normalized()
                              for root in self.referenced_by],
        }


@dataclass(frozen=True)
class ObjectClosureResult:
    """The closure verdict: entries, violations, pin description, digest."""

    commit_oid: str
    authorized_scope: ClosureScope
    entries: tuple[ClosureObjectEntry, ...]
    scope_violations: tuple[ScopeViolation, ...]
    status: str
    publishable: bool
    r_list_digest: str
    canonical_payload: dict[str, Any]
    retention_pin_request: dict[str, Any]

    def entries_with(self, status: str) -> tuple[ClosureObjectEntry, ...]:
        return tuple(entry for entry in self.entries
                     if entry.status == status)

    @property
    def present_entries(self) -> tuple[ClosureObjectEntry, ...]:
        return self.entries_with(STATUS_PRESENT)

    @property
    def gap_entries(self) -> tuple[ClosureObjectEntry, ...]:
        return tuple(entry for entry in self.entries
                     if entry.status in _GAP_STATUSES)

    @property
    def corrupt_entries(self) -> tuple[ClosureObjectEntry, ...]:
        return self.entries_with(STATUS_CORRUPT)

    @property
    def unsafe_entries(self) -> tuple[ClosureObjectEntry, ...]:
        return self.entries_with(STATUS_UNSAFE_PATH)


def _merge_references(
    references: list[ObjectReference],
) -> list[ObjectReference]:
    """Merge repeated logical ids; contradictory declarations fail closed."""
    groups: dict[str, list[ObjectReference]] = {}
    for reference in references:
        groups.setdefault(reference.logical_id, []).append(reference)

    merged: list[ObjectReference] = []
    for logical_id, group in groups.items():
        identity = {
            (
                ref.object_type,
                ref.sha256_digest,
                (ref.scope.kind, ref.scope.installation_id,
                 ref.scope.company_id),
                ref.version,
                ref.key_reference,
                ref.storage_class,
                ref.relative_path,
            )
            for ref in group
        }
        if len(identity) > 1:
            raise ObjectClosureError(
                f"conflicting declarations for logical object "
                f"{logical_id!r}: type/digest/scope/version/key/store/"
                "path must agree across its reference roots"
            )
        roots = tuple(sorted(
            {root for ref in group for root in ref.referenced_by},
            key=lambda r: (r.kind, r.root_id, r.coordinate),
        ))
        first = group[0]
        merged.append(ObjectReference(
            logical_id=first.logical_id,
            object_type=first.object_type,
            version=first.version,
            referenced_by=roots,
            scope=first.scope,
            sha256_digest=first.sha256_digest,
            key_reference=first.key_reference,
            storage_class=first.storage_class,
            relative_path=first.relative_path,
        ))
    return merged


def _violation_for(reference: ObjectReference,
                   reason: str) -> ScopeViolation:
    return ScopeViolation(
        logical_id=reference.logical_id,
        object_type=reference.object_type,
        version=reference.version,
        referenced_by=reference.referenced_by,
        scope=reference.scope,
        reason=reason,
        sha256_digest=reference.sha256_digest,
        key_reference=reference.key_reference,
    )


def _partition_by_scope(
    references: list[ObjectReference], authorized: ClosureScope
) -> tuple[list[ObjectReference], list[ScopeViolation], set[str]]:
    admitted: list[ObjectReference] = []
    violations: list[ScopeViolation] = []
    foreign_logical_ids: set[str] = set()
    for reference in references:
        reason = authorized.admits(reference.scope)
        if reason is None:
            admitted.append(reference)
        else:
            violations.append(_violation_for(reference, reason))
            foreign_logical_ids.add(reference.logical_id)
    return admitted, violations, foreign_logical_ids


def _lexical_relative_to(root, candidate) -> str:
    """Pure-string relative coordinate; resolves nothing on the filesystem."""
    root_parts = PurePosixPath(os.fspath(root)).parts
    cand_parts = PurePosixPath(os.fspath(candidate)).parts
    return "/".join(cand_parts[len(root_parts):])


def _is_root_confined(relative: str) -> bool:
    if not relative or relative.startswith("/"):
        return False
    for segment in relative.split("/"):
        if segment in ("", ".", ".."):
            return False
    return True


def _classify_verifier_failure(
    exc: BackupComponentValidationError,
) -> tuple[str, str]:
    """Map the pinned verifier's failure onto a closure status/issue code.

    The verifier remains the sole byte/path authority; this only labels
    its already-decided refusal. Missing verifier capabilities are not a
    per-object verdict — no honest verdict exists — so they raise.
    """
    message = str(exc).lower()
    if "capabilit" in message or "/proc" in message:
        raise ObjectClosureError(
            "content verification cannot run on this platform: pinned "
            "verifier capabilities or /proc channel are missing"
        ) from exc
    if "sha256 mismatch" in message:
        return STATUS_CORRUPT, "sha256_mismatch"
    if "size mismatch" in message:
        return STATUS_CORRUPT, "size_mismatch"
    if "[errno 2]" in message or "no such file" in message:
        return STATUS_MISSING, "object_missing"
    # On this kernel O_PATH|O_NOFOLLOW may hand back the symlink itself,
    # which the verifier then refuses as non-regular ("symlinks, ..."), or
    # fail the terminal open with ELOOP ("symbolic link"); both are the
    # same verdict: a link that must never be followed.
    if ("[errno 40]" in message or "symbolic link" in message
            or "symlink" in message):
        return STATUS_UNSAFE_PATH, "symbolic_link"
    if "[errno 21]" in message or "[errno 20]" in message \
            or "regular file" in message or "cannot be opened pinned" in message:
        return STATUS_UNSAFE_PATH, "not_a_regular_file"
    return STATUS_CORRUPT, "verification_failed"


def _open_pinned_root(path) -> int:
    try:
        return os.open(os.fspath(path), _ROOT_OPEN_FLAGS)
    except OSError as exc:
        raise ObjectClosureError(
            f"content store root cannot be opened pinned: {path}: {exc}"
        ) from exc


def _verify_archive(
    reference: ObjectReference, stores: ClosureStores, root_fd: int
) -> tuple[str, str | None, int | None, str | None]:
    """Return ``(status, issue, size_bytes, relative_path)`` for one object."""
    digest_hex = reference.digest_hex
    try:
        stored = stores.archive.get(digest_hex)
    except KeyError:
        return STATUS_MISSING, "object_missing", None, None
    try:
        disk_path = stores.archive.path_for(digest_hex)
    except KeyError:
        return STATUS_MISSING, "object_missing", None, None

    if reference.relative_path is not None:
        relative = _clean_relative_coordinate(
            reference.relative_path, "reference relative_path"
        )
        if not relative:
            return STATUS_UNSAFE_PATH, "path_traversal", None, None
    else:
        relative = _lexical_relative_to(stores.archive.objects_path,
                                        disk_path)
    if not _is_root_confined(relative):
        return STATUS_UNSAFE_PATH, "coordinate_escapes_root", None, None

    try:
        verify_backup_component(
            root_fd,
            relative_path=relative,
            size_bytes=stored.size_bytes,
            sha256_digest=f"{DIGEST_ALGORITHM}:{digest_hex}",
        )
    except BackupComponentValidationError as exc:
        status, issue = _classify_verifier_failure(exc)
        return status, issue, None, relative
    return STATUS_PRESENT, None, stored.size_bytes, relative


def _verify_mapping_object(
    reference: ObjectReference, stores: ClosureStores, root_fd: int
) -> tuple[str, str | None, int | None, str | None]:
    digest_hex = reference.digest_hex
    root = stores.mapping_objects.root
    relative = f"{digest_hex[:2]}/{digest_hex}"
    # lstat is a lexical, non-following probe against the configured
    # trusted root; it only supplies the declared size. A swapped symlink
    # is reported by lstat but still refused (never followed) by the
    # pinned verifier below.
    try:
        pre_stat = os.lstat(str(root / relative))
    except FileNotFoundError:
        return STATUS_MISSING, "object_missing", None, relative
    except OSError:
        return STATUS_UNSAFE_PATH, "path_unsafe", None, relative

    try:
        verify_backup_component(
            root_fd,
            relative_path=relative,
            size_bytes=pre_stat.st_size,
            sha256_digest=f"{DIGEST_ALGORITHM}:{digest_hex}",
        )
    except BackupComponentValidationError as exc:
        status, issue = _classify_verifier_failure(exc)
        return status, issue, None, relative
    return STATUS_PRESENT, None, pre_stat.st_size, relative


def _entry_for_reference(
    reference: ObjectReference, stores: ClosureStores,
    archive_root_fd: int, mapping_root_fd: int
) -> ClosureObjectEntry:
    if reference.sha256_digest is None:
        return ClosureObjectEntry(
            logical_id=reference.logical_id,
            object_type=reference.object_type,
            version=reference.version,
            referenced_by=reference.referenced_by,
            scope=reference.scope,
            status=STATUS_NOT_EXTERNALIZED,
            key_reference=reference.key_reference,
        )

    if reference.storage_class == STORAGE_MAPPING_OBJECTS:
        status, issue, size_bytes, relative = _verify_mapping_object(
            reference, stores, mapping_root_fd
        )
    else:
        status, issue, size_bytes, relative = _verify_archive(
            reference, stores, archive_root_fd
        )
    return ClosureObjectEntry(
        logical_id=reference.logical_id,
        object_type=reference.object_type,
        version=reference.version,
        referenced_by=reference.referenced_by,
        scope=reference.scope,
        status=status,
        sha256_digest=reference.sha256_digest,
        size_bytes=size_bytes,
        key_reference=reference.key_reference,
        storage_class=reference.storage_class,
        relative_path=relative,
        issue=issue,
    )


def _sorted_entries(entries: list[ClosureObjectEntry]
                    ) -> tuple[ClosureObjectEntry, ...]:
    return tuple(sorted(
        entries,
        key=lambda e: (
            e.object_type,
            e.logical_id,
            e.sha256_digest or "",
        ),
    ))


def _sorted_violations(violations: list[ScopeViolation]
                       ) -> tuple[ScopeViolation, ...]:
    return tuple(sorted(
        violations,
        key=lambda v: (
            v.logical_id,
            v.sha256_digest or "",
            v.reason,
        ),
    ))


def _build_payload(
    *, commit_oid: str, authorized: ClosureScope,
    entries: tuple[ClosureObjectEntry, ...],
    violations: tuple[ScopeViolation, ...],
) -> dict[str, Any]:
    return {
        "schema": CLOSURE_SCHEMA,
        "version": CLOSURE_VERSION,
        "commit_oid": commit_oid,
        "authorized_scope": authorized.normalized(),
        "entries": [entry.normalized() for entry in entries],
        "scope_violations": [violation.normalized()
                             for violation in violations],
    }


def _build_pin_request(entries: tuple[ClosureObjectEntry, ...]
                       ) -> dict[str, Any]:
    objects = sorted(
        (
            {
                "storage_class": entry.storage_class,
                "relative_path": entry.relative_path,
                "sha256_digest": entry.sha256_digest,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
            if entry.status == STATUS_PRESENT
        ),
        key=lambda item: (item["storage_class"], item["relative_path"]),
    )
    return {
        "kind": "retention_pin_description",
        "created_pin": False,
        "note": "description only; the maintenance-barrier assembly "
                "creates and holds the actual GC-protection pin",
        "objects": objects,
    }


def _overall_status(entries: tuple[ClosureObjectEntry, ...],
                    violations: tuple[ScopeViolation, ...]) -> str:
    if violations:
        return CLOSURE_SCOPE_VIOLATION
    statuses = {entry.status for entry in entries}
    if STATUS_CORRUPT in statuses:
        return CLOSURE_CORRUPT
    if STATUS_UNSAFE_PATH in statuses:
        return CLOSURE_UNSAFE_PATHS
    if statuses & _GAP_STATUSES:
        return CLOSURE_INCOMPLETE
    return CLOSURE_COMPLETE


def collect_object_closure(
    *,
    commit_oid: str,
    authorized_scope: ClosureScope,
    references: list[ObjectReference] | tuple[ObjectReference, ...],
    stores: ClosureStores,
    object_format: str = "sha1",
) -> ObjectClosureResult:
    """Collect and verify the R closure for one fixed H + W reference set.

    Pure and read-only: no pin, lock, checkpoint, copy, GC, Git open, DB
    write or network call. Out-of-scope references are refused before
    any object byte is read; otherwise every externalized reference is
    located in its content-addressed store and byte-verified through
    the pinned component verifier. Returns the full structured verdict
    (present/missing/not-externalized/corrupt/unsafe entries, scope
    violations, a retention-pin description and the deterministic R-list
    digest); only malformed calls raise :class:`ObjectClosureError`.
    """
    _require_str(commit_oid, "commit_oid")
    _require_commit_oid(commit_oid, "commit_oid", object_format)
    if not isinstance(authorized_scope, ClosureScope):
        raise ObjectClosureError(
            "authorized_scope must be a ClosureScope"
        )
    if not isinstance(references, (list, tuple)):
        raise ObjectClosureError("references must be a list or tuple")
    if any(not isinstance(ref, ObjectReference) for ref in references):
        raise ObjectClosureError(
            "every reference must be an ObjectReference"
        )
    if not isinstance(stores, ClosureStores):
        raise ObjectClosureError("stores must be a ClosureStores")

    merged = _merge_references(list(references))
    admitted, violations, foreign_ids = _partition_by_scope(
        merged, authorized_scope
    )
    # A logical id seen under both the authorized and a foreign scope is
    # contradictory: drop the in-scope variant too rather than admit half
    # of it, and record the contradiction explicitly.
    admitted = [ref for ref in admitted
                if ref.logical_id not in foreign_ids]
    for logical_id in sorted(foreign_ids):
        twins = {ref for ref in merged
                 if ref.logical_id == logical_id
                 and authorized_scope.admits(ref.scope) is None}
        for ref in sorted(twins, key=lambda r: r.sha256_digest or ""):
            violations.append(_violation_for(
                ref, "conflicting_scope_declarations"
            ))
    violations = list(_sorted_violations(violations))

    entries: list[ClosureObjectEntry] = []
    if not violations:
        # Scope refusal happens before a single object byte is read; the
        # roots are not even opened in that outcome.
        archive_root_fd = _open_pinned_root(stores.archive.objects_path)
        mapping_root_fd = _open_pinned_root(stores.mapping_objects.root)
        try:
            for reference in sorted(
                    admitted,
                    key=lambda r: (r.object_type, r.logical_id,
                                   r.sha256_digest or "")):
                entries.append(_entry_for_reference(
                    reference, stores,
                    archive_root_fd, mapping_root_fd,
                ))
        finally:
            for fd in (archive_root_fd, mapping_root_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass

    ordered_entries = _sorted_entries(entries)
    ordered_violations = _sorted_violations(violations)
    payload = _build_payload(
        commit_oid=commit_oid,
        authorized=authorized_scope,
        entries=ordered_entries,
        violations=ordered_violations,
    )
    # Same digest rule as backup manifests: canonical record
    # serialization (sorted keys at every level) plus SHA-256.
    r_list_digest = compute_core_digest(payload)
    status = _overall_status(ordered_entries, ordered_violations)
    return ObjectClosureResult(
        commit_oid=commit_oid,
        authorized_scope=authorized_scope,
        entries=ordered_entries,
        scope_violations=ordered_violations,
        status=status,
        publishable=status == CLOSURE_COMPLETE,
        r_list_digest=r_list_digest,
        canonical_payload=payload,
        retention_pin_request=_build_pin_request(ordered_entries),
    )
