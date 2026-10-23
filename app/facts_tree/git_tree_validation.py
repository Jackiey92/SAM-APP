"""Pure validator for facts Git tree entries and caller path whitelists.

Approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f,
F-001 P01 section 10.2.3 (a commit tree may only carry canonical paths
and file types: the model picks object handles and actions but can never
submit a file path, traversal, symlink, Git submodule or executable
file) and section 14.2 (a tree carrying unknown file types must not be
accepted).

Public entry point:

    validate_tree_entries(entries, *, expected_paths, object_format)

Entry structure (fixed, one shape for all callers)
--------------------------------------------------
``entries`` is an iterable of entries; each entry must be a tuple of
exactly four items ``(mode, path, oid, content_bytes)``:

- ``mode``: str, the Git entry mode. Only ``"100644"`` (a regular
  non-executable file) is accepted; symlinks (``"120000"``),
  executables (``"100755"``), Git submodules (``"160000"``),
  directories and any other mode are rejected.
- ``path``: str, the repository-relative entry path. It must be unique
  across entries, clean (non-empty, not absolute, no empty segment, no
  ``.`` or ``..`` segment, no backslash, no NUL) and present in the
  caller-supplied ``expected_paths`` whitelist. Across entries, no path
  may be a proper slash-delimited directory prefix of another entry
  path: a file and its own subtree cannot coexist in one tree. This
  cross-entry invariant cannot be seen by checking each path alone.
- ``oid``: str, the object id. Only its shape is checked: a full
  lowercase hex string of 40 characters for ``sha1`` or 64 characters
  for ``sha256``.
- ``content_bytes``: bytes, the entry content. It must parse as a JSON
  object via ``git_format.loads_record`` and re-serialize via
  ``git_format.dumps_record`` to exactly the same bytes; illegal JSON,
  a non-object payload and any non-canonical encoding are rejected.

``expected_paths`` is an iterable of str paths forming the exact
whitelist: the entries must cover precisely this set, so a missing,
duplicate or extra path is rejected. The whitelist itself is validated
with the same path rules and must not contain duplicates.
``object_format`` must be the str ``"sha1"`` or ``"sha256"``.

Return and error contract
-------------------------
Returns None on success and never mutates its inputs. TypeError signals
wrong types (a non-tuple entry, a non-str mode/path/oid, non-bytes
content, a non-str whitelist path, a non-str object_format, or a bare
str/bytes passed where an iterable of entries or paths is expected);
ValueError signals wrong values (a tuple entry of the wrong length, an
unsupported object_format, a forbidden or unknown mode, an unsafe,
duplicated, missing or extra path, a file path that is also a proper
directory prefix of another entry path, a malformed OID, illegal,
non-object or non-canonical content bytes, or a duplicated or unsafe
whitelist path). Problems are reported in a fixed order: the object
format first, then the whitelist, then each entry in input order (mode,
path safety, duplicate, whitelist membership, OID, content), then the
cross-entry file/directory prefix-conflict check over the full entry
path set (scanned in sorted path order, so the reported conflict is
independent of input order), then the missing-paths comparison.

Boundary (explicitly NOT done here):

- This is a pure function. It never queries a Git object store, so a
  well-shaped OID is not proof that the object exists or that its Git
  type is actually blob; it never reads or writes files, creates a
  repository, touches a ref or accesses SQLite.
- It does not validate any business record schema, identity or
  authorization, reference closure, the unique-leaf invariant or commit
  history; those belong to the host and the Commit Broker path.
"""
from app.facts_tree import git_format

_OID_HEX_LENGTHS = {"sha1": 40, "sha256": 64}
_ALLOWED_MODE = "100644"
_HEX_DIGITS = frozenset("0123456789abcdef")


def validate_tree_entries(entries, *, expected_paths, object_format):
    """Validate tree entries against the caller's exact path whitelist.

    See the module docstring for the fixed entry structure
    ``(mode, path, oid, content_bytes)``, the whitelist semantics and
    the full return/error contract. Returns None on success.
    """
    oid_length = _check_object_format(object_format)
    whitelist = _check_whitelist(expected_paths)
    seen = set()
    for entry in _iterate(entries):
        mode, path, oid, content = _unpack(entry)
        _check_mode(mode)
        _check_path("tree entry path", path)
        if path in seen:
            raise ValueError("tree entry path %r is duplicated" % path)
        if path not in whitelist:
            raise ValueError(
                "tree entry path %r is not in the caller whitelist" % path)
        seen.add(path)
        _check_oid(path, oid, oid_length)
        _check_content(path, content)
    _check_prefix_conflicts(seen)
    missing = sorted(whitelist - seen)
    if missing:
        raise ValueError(
            "tree is missing whitelisted paths: %s" % (missing,))
    return None


def _check_object_format(object_format):
    if not isinstance(object_format, str):
        raise TypeError(
            "object_format must be a str, got %s"
            % type(object_format).__name__)
    if object_format not in _OID_HEX_LENGTHS:
        raise ValueError(
            "unsupported object_format %r: only 'sha1' and 'sha256' are "
            "supported" % (object_format,))
    return _OID_HEX_LENGTHS[object_format]


def _check_whitelist(expected_paths):
    # A bare str would otherwise silently iterate into single-character
    # "paths"; it is a caller type error, not a whitelist.
    if isinstance(expected_paths, (str, bytes)):
        raise TypeError(
            "expected_paths must be an iterable of str paths, not a "
            "single %s" % type(expected_paths).__name__)
    try:
        items = list(expected_paths)
    except TypeError:
        raise TypeError(
            "expected_paths must be an iterable of str paths") from None
    seen = set()
    for path in items:
        _check_path("whitelist path", path)
        if path in seen:
            raise ValueError("whitelist path %r is duplicated" % path)
        seen.add(path)
    return seen


def _iterate(entries):
    if isinstance(entries, (str, bytes)):
        raise TypeError(
            "entries must be an iterable of (mode, path, oid, "
            "content_bytes) tuples, not a single %s"
            % type(entries).__name__)
    try:
        return iter(entries)
    except TypeError:
        raise TypeError(
            "entries must be an iterable of (mode, path, oid, "
            "content_bytes) tuples") from None


def _unpack(entry):
    if not isinstance(entry, tuple):
        raise TypeError(
            "tree entry must be a tuple (mode, path, oid, "
            "content_bytes), got %s" % type(entry).__name__)
    if len(entry) != 4:
        raise ValueError(
            "tree entry must have exactly 4 items (mode, path, oid, "
            "content_bytes), got %d" % len(entry))
    return entry


def _check_mode(mode):
    if not isinstance(mode, str):
        raise TypeError(
            "tree entry mode must be a str, got %s"
            % type(mode).__name__)
    if mode != _ALLOWED_MODE:
        raise ValueError(
            "tree entry mode %r is not allowed: only %r (a regular "
            "non-executable file) is accepted; symlinks, executables, "
            "Git submodules and directories are rejected"
            % (mode, _ALLOWED_MODE))


def _check_path(kind, path):
    # kind is "tree entry path" or "whitelist path", used in messages.
    if not isinstance(path, str):
        raise TypeError(
            "%s must be a str, got %s" % (kind, type(path).__name__))
    if not path:
        raise ValueError("%s must not be empty" % kind)
    if "\x00" in path:
        raise ValueError("%s %r contains a NUL character" % (kind, path))
    if "\\" in path:
        raise ValueError("%s %r contains a backslash" % (kind, path))
    if path.startswith("/"):
        raise ValueError(
            "%s %r is absolute, not repository-relative" % (kind, path))
    for segment in path.split("/"):
        if segment == "":
            raise ValueError(
                "%s %r contains an empty segment" % (kind, path))
        if segment in (".", ".."):
            raise ValueError(
                "%s %r contains a traversal segment %r"
                % (kind, path, segment))


def _check_prefix_conflicts(seen):
    # A file and its own subtree cannot coexist in one tree: no entry
    # path may be a proper slash-delimited directory prefix of another
    # entry path. Per-path checks cannot see this conflict, and checking
    # only adjacent paths in sorted order is not enough either: an
    # unrelated sibling such as "a-b" sorts between "a" and
    # "a/child.json" because "-" < "/", hiding the conflict from
    # pairwise checks. Compare every proper directory ancestor of each
    # path against the full path set instead; iterating in sorted order
    # keeps the reported conflict independent of the caller's entry
    # order.
    for path in sorted(seen):
        segments = path.split("/")
        for depth in range(1, len(segments)):
            ancestor = "/".join(segments[:depth])
            if ancestor in seen:
                raise ValueError(
                    "tree entry paths %r and %r conflict: a file cannot "
                    "also be a directory" % (ancestor, path))


def _check_oid(path, oid, oid_length):
    if not isinstance(oid, str):
        raise TypeError(
            "tree entry %r oid must be a str, got %s"
            % (path, type(oid).__name__))
    if len(oid) != oid_length:
        raise ValueError(
            "tree entry %r oid must be exactly %d hex characters, got %d"
            % (path, oid_length, len(oid)))
    if any(char not in _HEX_DIGITS for char in oid):
        raise ValueError(
            "tree entry %r oid must be lowercase hex" % path)


def _check_content(path, content):
    if not isinstance(content, bytes):
        raise TypeError(
            "tree entry %r content must be bytes, got %s"
            % (path, type(content).__name__))
    try:
        parsed = git_format.loads_record(content)
    except UnicodeDecodeError as exc:
        raise ValueError(
            "tree entry %r content is not valid UTF-8" % path) from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "tree entry %r content is not a readable record: %s"
            % (path, exc)) from exc
    if git_format.dumps_record(parsed) != content:
        raise ValueError(
            "tree entry %r content is not the canonical serialization "
            "of its payload" % path)
