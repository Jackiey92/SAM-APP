"""Host-local projection pointers and atomic publication (appendix D3 v1.2)."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from app.facts_tree.git_format import encode_id
from app.facts_tree.projection_generation import BuiltProjectionGeneration


_META_KEYS = frozenset({
    "generation_id", "repo_id", "company_id", "projected_commit", "head_oid",
    "object_format", "record_count", "active_fact_count", "render_sha256",
    "projection_schema_version", "builder_version", "renderer_version",
    "required_indexes", "status",
})
_POINTER_KEYS = frozenset({
    "format", "repo_id", "company_id", "generation_id", "db_path",
    "projected_commit", "render_sha256", "published_at",
})
_NUMBERS = ("record_count", "active_fact_count", "projection_schema_version",
            "builder_version", "renderer_version")


class ProjectionRouterError(Exception):
    """Base class for host projection pointer failures."""


class NoCurrentProjection(ProjectionRouterError):
    """No current pointer has been published for this scope yet."""


class CurrentProjectionUnavailable(ProjectionRouterError):
    """Current pointer is corrupt, missing its generation, tampered, or not ready."""


@dataclass(frozen=True)
class ServingGeneration:
    generation_id: str
    db_path: str
    repo_id: str
    company_id: str
    projected_commit: str
    head_oid: str
    object_format: str
    render_sha256: str
    record_count: int
    active_fact_count: int
    projection_schema_version: int
    builder_version: int
    renderer_version: int
    required_indexes: tuple[str, ...]


def _path(value, name):
    if not isinstance(value, (str, os.PathLike)) or type(os.fspath(value)) is not str:
        raise TypeError(name + " must be str or os.PathLike[str]")
    if not os.fspath(value):
        raise ValueError(name + " must not be empty")
    return Path(value)


def _scope(root, repo_id, company_id):
    for value, name in ((repo_id, "repo_id"), (company_id, "company_id")):
        if type(value) is not str:
            raise TypeError(name + " must be str")
        encode_id(value)
    return _path(root, "root") / encode_id(repo_id) / encode_id(company_id) / "current.json"


def _hex(value, length):
    return (type(value) is str and len(value) == length
            and all(char in "0123456789abcdefABCDEF" for char in value))


def _integer(value, name):
    if type(value) is not int:
        raise TypeError(name + " must be int")
    if not 0 <= value <= 2**63 - 1:
        raise ValueError(name + " is outside SQLite integer range")


def _no_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symbolic links are not allowed: " + str(path))


def _regular(path):
    _no_symlinks(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("not a regular file: " + str(path))
    return resolved


def _within(db, directory):
    if db == directory or not db.is_relative_to(directory):
        raise ValueError("generation is outside generations_dir: " + str(db))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError("non-finite JSON constant: " + value)


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_bad_constant)


def _timestamp(value):
    if type(value) is not str:
        raise TypeError("published_at must be str")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("published_at must be UTC ISO8601")
    return value


def _message(repo, company, generation, detail):
    return f"projection scope=({repo!r}, {company!r}) generation={generation!r}: {detail}"


def _pointer(path, repo_id, company_id):
    """Read only the published selection; never infer a selection from files."""
    identifier = "unknown"
    try:
        # lexists distinguishes an absent pointer from a dangling symlink.
        if not os.path.lexists(path):
            # A corrupt/non-directory/symlinked scope must not look unpublished.
            _no_symlinks(path.parent)
            for parent in path.parents:
                if parent.exists() and not parent.is_dir():
                    raise ValueError("pointer parent is not a directory")
            raise NoCurrentProjection(_message(repo_id, company_id, identifier, "no current pointer"))
        _regular(path)
        pointer = _json(path.read_text(encoding="utf-8"))
        if type(pointer) is not dict or set(pointer) != _POINTER_KEYS:
            raise ValueError("current pointer must contain exactly eight keys")
        identifier = pointer.get("generation_id", "unknown")
        if any(type(value) is not str for value in pointer.values()):
            raise ValueError("current pointer values must be strings")
        if (pointer["format"] != "projection-current@1"
                or pointer["repo_id"] != repo_id or pointer["company_id"] != company_id
                or not _hex(identifier, 32) or not _hex(pointer["render_sha256"], 64)):
            raise ValueError("current pointer format/scope/identity mismatch")
        db = Path(pointer["db_path"])
        if not db.is_absolute() or str(db.resolve()) != pointer["db_path"]:
            raise ValueError("current db_path must be resolved and absolute")
        _timestamp(pointer["published_at"])
        return pointer
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise CurrentProjectionUnavailable(_message(repo_id, company_id, identifier, str(exc))) from exc


def _meta(connection, db, repo_id, company_id):
    rows = connection.execute("SELECT key, value FROM generation_meta").fetchall()
    if (len(rows) != 14 or any(type(key) is not str or type(value) is not str
                               for key, value in rows)):
        raise ValueError("generation_meta must contain fourteen TEXT keys/values")
    meta = dict(rows)
    if set(meta) != _META_KEYS or meta["status"] != "ready":
        raise ValueError("generation_meta keys/status mismatch")
    if meta["repo_id"] != repo_id or meta["company_id"] != company_id:
        raise ValueError("generation scope mismatch")
    identifier = meta["generation_id"]
    if not _hex(identifier, 32) or db.name != "gen-" + identifier + ".sqlite":
        raise ValueError("generation filename/id mismatch")
    if not _hex(meta["render_sha256"], 64):
        raise ValueError("invalid render_sha256")
    oid_length = {"sha1": 40, "sha256": 64}.get(meta["object_format"])
    if oid_length is None or any(not _hex(meta[key], oid_length)
                                 for key in ("projected_commit", "head_oid")):
        raise ValueError("invalid object_format/commit identity")
    numbers = {}
    for key in _NUMBERS:
        value = meta[key]
        if not value.isascii() or not value.isdecimal():
            raise ValueError(key + " must be a decimal TEXT integer")
        numbers[key] = int(value)
        _integer(numbers[key], key)
    if any(numbers[key] != 1 for key in _NUMBERS[2:]):
        raise ValueError("unsupported projection version")
    indexes = _json(meta["required_indexes"])
    if type(indexes) is not list or indexes != ["canonical_records"]:
        raise ValueError("unsupported required_indexes")
    return ServingGeneration(
        identifier, str(db), repo_id, company_id, meta["projected_commit"],
        meta["head_oid"], meta["object_format"], meta["render_sha256"],
        **numbers, required_indexes=tuple(indexes))


def _verify(connection, db, repo_id, company_id):
    """Stream bytes in path order; do not parse or interpret canonical payloads."""
    connection.execute("BEGIN")
    serving = _meta(connection, db, repo_id, company_id)
    render = sha256()
    count = 0
    for path, family, content, length, digest in connection.execute(
            "SELECT path, family, canonical_bytes, byte_length, sha256 "
            "FROM record_canonical ORDER BY path"):
        if (type(path) is not str or type(family) is not str
                or type(content) is not bytes or type(length) is not int
                or len(content) != length or sha256(content).hexdigest() != digest):
            raise ValueError("canonical byte length/checksum mismatch")
        if count:
            render.update(b"\x1e")
        render.update(family.encode("utf-8") + b"\x1f" + path.encode("utf-8") + b"\x1f")
        render.update(content)
        count += 1
    if render.hexdigest() != serving.render_sha256 or count != serving.record_count:
        raise ValueError("canonical render_sha256/record_count mismatch")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("generation integrity_check failed")
    return serving


def _connect(db):
    return sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)


def _identity(serving):
    return {
        "format": "projection-current@1", "repo_id": serving.repo_id,
        "company_id": serving.company_id, "generation_id": serving.generation_id,
        "db_path": serving.db_path, "projected_commit": serving.projected_commit,
        "render_sha256": serving.render_sha256,
    }


def _atomic_pointer(path, pointer):
    # Persist newly created directory entries as well as the pointer itself.
    _no_symlinks(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _no_symlinks(path.parent)
    for directory in (path.parent.parent.parent, path.parent.parent):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".current.json.tmp-{os.getpid()}-", dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(pointer, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        temp = None
        os.fsync(directory_fd)
    finally:
        if temp is not None:
            temp.unlink()
        os.close(directory_fd)


def publish_generation(built, root, generations_dir, *, expected_repo_id: str,
                       expected_company_id: str, now=None) -> ServingGeneration:
    """Validate a ready generation before atomically selecting it for one scope."""
    pointer_path = _scope(root, expected_repo_id, expected_company_id)
    directory = _path(generations_dir, "generations_dir")
    if type(built) is not BuiltProjectionGeneration:
        raise TypeError("built must be BuiltProjectionGeneration")
    for name in ("db_path", "generation_id", "projected_commit", "render_sha256"):
        if type(getattr(built, name)) is not str:
            raise TypeError("built." + name + " must be str")
    _path(built.db_path, "built.db_path")
    _integer(built.record_count, "built.record_count")
    if type(built.ready) is not bool:
        raise TypeError("built.ready must be bool")
    if built.ready is not True:
        raise ValueError("built must be ready")
    if now is not None and not callable(now):
        raise TypeError("now must be callable")
    try:
        db = _regular(Path(built.db_path))
        _within(db, directory.resolve())
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(_message(expected_repo_id, expected_company_id,
                                  built.generation_id, str(exc))) from exc
    lock_fd = None
    try:
        root_path = _path(root, "root")
        _no_symlinks(root_path)
        root_path.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Repeat path checks under the publication lock, before opening SQLite.
        db = _regular(Path(built.db_path))
        _within(db, directory.resolve())
        try:
            with closing(_connect(db)) as connection:
                serving = _verify(connection, db, expected_repo_id, expected_company_id)
            if (serving.generation_id != built.generation_id
                    or serving.projected_commit != built.projected_commit
                    or serving.render_sha256 != built.render_sha256
                    or serving.record_count != built.record_count):
                raise ValueError("built identity/count does not match generation_meta")
        except (ValueError, TypeError) as exc:
            raise ProjectionRouterError(_message(expected_repo_id, expected_company_id,
                                                 built.generation_id, str(exc))) from exc
        identity = _identity(serving)
        try:
            old = _pointer(pointer_path, expected_repo_id, expected_company_id)
        except NoCurrentProjection:
            old = None
        if old is not None and {key: old[key] for key in identity} == identity:
            return serving
        stamp = now() if now is not None else datetime.now(timezone.utc).isoformat()
        _timestamp(stamp)
        _atomic_pointer(pointer_path, {**identity, "published_at": stamp})
        return serving
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise ProjectionRouterError(_message(expected_repo_id, expected_company_id,
                                             built.generation_id, str(exc))) from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def load_current(root, *, repo_id: str, company_id: str) -> ServingGeneration:
    """Read and fully revalidate exactly the published generation, or fail closed."""
    pointer = _pointer(_scope(root, repo_id, company_id), repo_id, company_id)
    try:
        db = _regular(Path(pointer["db_path"]))
        with closing(_connect(db)) as connection:
            serving = _verify(connection, db, repo_id, company_id)
        if any(pointer[key] != value for key, value in _identity(serving).items()):
            raise ValueError("current pointer/generation identity mismatch")
        return serving
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        raise CurrentProjectionUnavailable(_message(repo_id, company_id,
                                                    pointer["generation_id"], str(exc))) from exc


def open_current(root, *, repo_id: str, company_id: str):
    """Return a validated read-only snapshot; the caller owns its connection."""
    serving = load_current(root, repo_id=repo_id, company_id=company_id)
    connection = None
    try:
        db = _regular(Path(serving.db_path))
        connection = _connect(db)
        # Validate this connection too, closing the load/open replacement window.
        if _verify(connection, db, repo_id, company_id) != serving:
            raise ValueError("generation changed while opening current")
        return serving, connection
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        if connection is not None:
            connection.close()
        raise CurrentProjectionUnavailable(_message(repo_id, company_id,
                                                    serving.generation_id, str(exc))) from exc


def recover_published(root, generations_dir, *, repo_id: str,
                      company_id: str) -> ServingGeneration | None:
    """Recover only a published pointer, never choose or remove a generation."""
    _scope(root, repo_id, company_id)
    directory = _path(generations_dir, "generations_dir")
    try:
        serving = load_current(root, repo_id=repo_id, company_id=company_id)
    except NoCurrentProjection:
        return None
    try:
        _within(Path(serving.db_path).resolve(), directory.resolve())
    except (OSError, ValueError, RuntimeError) as exc:
        raise CurrentProjectionUnavailable(_message(repo_id, company_id,
                                                    serving.generation_id, str(exc))) from exc
    return serving


def find_retire_candidates(root, generations_dir, *, repo_id: str,
                           company_id: str) -> tuple[str, ...]:
    """Discover unselected ready IDs by metadata only; never modify the disk."""
    pointer_path = _scope(root, repo_id, company_id)
    directory = _path(generations_dir, "generations_dir")
    try:
        current = _pointer(pointer_path, repo_id, company_id)["generation_id"]
    except NoCurrentProjection:
        current = None
    result = []
    try:
        entries = tuple(directory.iterdir()) if directory.exists() else ()
        for path in entries:
            if (not path.name.startswith("gen-") or not path.name.endswith(".sqlite")
                    or not _hex(path.name[4:-7], 32)):
                continue
            try:
                db = _regular(path)
                with closing(_connect(db)) as connection:
                    serving = _meta(connection, db, repo_id, company_id)
                if serving.generation_id != current:
                    result.append(serving.generation_id)
            except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
                continue
    except OSError as exc:
        raise CurrentProjectionUnavailable(_message(repo_id, company_id, current, str(exc))) from exc
    return tuple(sorted(result))
