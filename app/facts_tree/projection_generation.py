"""Deterministic, standalone canonical SQLite generations (appendix D-2 v1).

Inputs have already passed schema/closure validation. This layer checks their
assembly, stores the original bytes, and verifies committed data through fresh
read-only connections. No business payload is parsed or reconstructed here.
"""
from collections import Counter
from collections.abc import Mapping, Set
from contextlib import closing
from dataclasses import dataclass, replace
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from app.facts_tree import git_format
from app.facts_tree.git_repository import SnapshotRecord
from app.facts_tree.projection_plan import ProjectionPlan


PROJECTION_SCHEMA_VERSION = 1
PROJECTION_BUILDER_VERSION = 1
PROJECTION_RENDERER_VERSION = 1

_DDL = (
    "CREATE TABLE generation_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE record_canonical (path TEXT PRIMARY KEY, family TEXT NOT NULL, "
    "canonical_bytes BLOB NOT NULL, byte_length INTEGER NOT NULL, sha256 TEXT NOT NULL)",
    "CREATE INDEX idx_record_canonical_family ON record_canonical(family)",
    "CREATE TABLE family_counts (family TEXT PRIMARY KEY, record_count INTEGER NOT NULL)",
    "CREATE TABLE fact_current_view (fact_uid TEXT PRIMARY KEY, revision INTEGER NOT NULL, "
    "is_active INTEGER NOT NULL CHECK (is_active IN (0,1)))",
    "CREATE TABLE placement_current_view (fact_uid TEXT PRIMARY KEY, revision INTEGER NOT NULL)",
)
_TABLES = {"generation_meta", "record_canonical", "family_counts",
           "fact_current_view", "placement_current_view"}


@dataclass(frozen=True)
class BuiltProjectionGeneration:
    db_path: str
    generation_id: str
    projected_commit: str
    render_sha256: str
    record_count: int
    ready: bool


def _integer(value, name, minimum=0):
    if type(value) is not int:
        raise TypeError(name + " must be an int")
    if not minimum <= value <= 2**63 - 1:
        raise ValueError(name + " is outside SQLite integer range")


def _assemble(plan, records, active_fact_uids, expected_repo_id, expected_company_id):
    if type(plan) is not ProjectionPlan:
        raise TypeError("plan must be a ProjectionPlan")
    for name in ("repo_id", "company_id", "commit_oid", "head_oid", "object_format",
                 "render_sha256"):
        if type(getattr(plan, name)) is not str:
            raise TypeError(name + " must be a str")
    if (expected_repo_id != plan.repo_id or expected_company_id != plan.company_id):
        raise ValueError("expected repo/company does not match plan")
    if type(plan.render_bytes) is not bytes:
        raise TypeError("plan.render_bytes must be bytes")
    _integer(plan.total_records, "total_records")
    _integer(plan.active_fact_count, "active_fact_count")
    mappings = {}
    for name in ("family_counts", "current_heads", "primary_placement"):
        mapping = getattr(plan, name)
        if not isinstance(mapping, Mapping):
            raise TypeError(name + " must be a Mapping")
        mappings[name] = dict(mapping)
        for key, value in mappings[name].items():
            if type(key) is not str:
                raise TypeError(name + " keys must be str")
            _integer(value, name, minimum=1)
    # Snapshot caller-owned mappings/sets before IO; never mutate inputs.
    plan = replace(plan, **mappings)
    if not isinstance(active_fact_uids, Set):
        raise TypeError("active_fact_uids must be an AbstractSet[str]")
    if any(type(uid) is not str for uid in active_fact_uids):
        raise TypeError("active_fact_uids must contain str")
    active = frozenset(active_fact_uids)
    if not active <= plan.current_heads.keys() or len(active) != plan.active_fact_count:
        raise ValueError("active set is outside current_heads or has incorrect count")
    contents = {}
    for record in records:
        if isinstance(record, SnapshotRecord):
            path, content = record.path, record.content
        else:
            try:
                path, content = record
            except (TypeError, ValueError) as exc:
                raise TypeError("record must be a (path, bytes) pair or SnapshotRecord") from exc
        if type(path) is not str or type(content) is not bytes:
            raise TypeError("record path/content must be str/bytes")
        if path in contents:
            raise ValueError("duplicate record path: " + path)
        family = git_format.canonical_record_type(path)
        if family is None:
            raise ValueError("record path is not canonical: " + path)
        contents[path] = (family, content)
    rows, segments = [], []
    for path, (family, content) in sorted(contents.items()):
        rows.append((path, family, content, len(content), sha256(content).hexdigest()))
        segments.append(family.encode("utf-8") + b"\x1f" + path.encode("utf-8")
                        + b"\x1f" + content)
    render = b"\x1e".join(segments)
    if render != plan.render_bytes or sha256(render).hexdigest() != plan.render_sha256:
        raise ValueError("records and plan render do not match")
    counts = dict(Counter(row[1] for row in rows))
    if (counts != plan.family_counts or len(rows) != plan.total_records
            or sum(plan.family_counts.values()) != plan.total_records):
        raise ValueError("record/family counts do not match plan")
    return plan, rows, active


def _metadata(plan, generation_id):
    return {
        "generation_id": generation_id, "repo_id": plan.repo_id,
        "company_id": plan.company_id, "projected_commit": plan.commit_oid,
        "head_oid": plan.head_oid, "object_format": plan.object_format,
        "record_count": str(plan.total_records),
        "active_fact_count": str(plan.active_fact_count),
        "render_sha256": plan.render_sha256,
        "projection_schema_version": str(PROJECTION_SCHEMA_VERSION),
        "builder_version": str(PROJECTION_BUILDER_VERSION),
        "renderer_version": str(PROJECTION_RENDERER_VERSION),
        "required_indexes": json.dumps(["canonical_records"], sort_keys=True,
                                       separators=(",", ":")),
    }


def _verify(path, plan, rows, active, meta):
    """Read every stored value on a new mode=ro connection, never the writer."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != _TABLES:
            raise ValueError("generation table set does not match")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("generation integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("generation foreign_key_check failed")
        stored = connection.execute("SELECT * FROM record_canonical ORDER BY path").fetchall()
        if stored != rows:
            raise ValueError("canonical records do not match")
        segments = []
        counts = Counter()
        for record_path, family, content, length, digest in stored:
            if (type(content) is not bytes or len(content) != length
                    or sha256(content).hexdigest() != digest
                    or git_format.canonical_record_type(record_path) != family):
                raise ValueError("canonical record checksum/length/family mismatch")
            counts[family] += 1
            segments.append(family.encode("utf-8") + b"\x1f"
                            + record_path.encode("utf-8") + b"\x1f" + content)
        if (sha256(b"\x1e".join(segments)).hexdigest() != meta["render_sha256"]
                or len(stored) != plan.total_records or dict(counts) != plan.family_counts):
            raise ValueError("generation render/count mismatch")
        if connection.execute("SELECT * FROM family_counts ORDER BY family").fetchall() != sorted(
                plan.family_counts.items()):
            raise ValueError("generation family_counts mismatch")
        expected_heads = [(uid, revision, int(uid in active))
                          for uid, revision in sorted(plan.current_heads.items())]
        if connection.execute("SELECT * FROM fact_current_view ORDER BY fact_uid").fetchall() != expected_heads:
            raise ValueError("generation current_heads/active mismatch")
        if connection.execute("SELECT * FROM placement_current_view ORDER BY fact_uid").fetchall() != sorted(
                plan.primary_placement.items()):
            raise ValueError("generation primary_placement mismatch")
        if connection.execute("SELECT * FROM generation_meta ORDER BY key").fetchall() != sorted(meta.items()):
            raise ValueError("generation metadata mismatch")
        indexes = connection.execute("PRAGMA index_info(idx_record_canonical_family)").fetchall()
        if indexes != [(0, 1, "family")]:
            raise ValueError("generation family index mismatch")


def _sync_file(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _write_records(path, plan, rows, active, meta):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _DDL:
                connection.execute(statement)
            connection.executemany("INSERT INTO record_canonical VALUES (?, ?, ?, ?, ?)", rows)
            connection.executemany("INSERT INTO family_counts VALUES (?, ?)", sorted(plan.family_counts.items()))
            connection.executemany("INSERT INTO fact_current_view VALUES (?, ?, ?)", [
                (uid, revision, int(uid in active)) for uid, revision in sorted(plan.current_heads.items())])
            connection.executemany("INSERT INTO placement_current_view VALUES (?, ?)", sorted(plan.primary_placement.items()))
            connection.executemany("INSERT INTO generation_meta VALUES (?, ?)", sorted(meta.items()))


def build_projection_generation(plan: ProjectionPlan, records, active_fact_uids, directory,
                                *, expected_repo_id: str, expected_company_id: str
                                ) -> BuiltProjectionGeneration:
    """Build and verify one immutable generation, or read-verify an existing one.

    A directory flock serializes cooperating builders (including threads), so
    the required os.replace cannot overwrite a concurrently published file.
    All data/DDL are committed together. The absence of status marks staging;
    ready is inserted only after both staging and final-name read-only checks.
    Any failure removes only files created by this call, never an existing gen.
    """
    plan, rows, active = _assemble(plan, records, active_fact_uids,
                                   expected_repo_id, expected_company_id)
    if not isinstance(directory, (str, os.PathLike)) or not isinstance(os.fspath(directory), str):
        raise TypeError("directory must be str or os.PathLike[str]")
    if not os.fspath(directory):
        raise ValueError("directory must not be empty")
    generation_id = sha256("\n".join((
        plan.repo_id, plan.company_id, plan.commit_oid, plan.render_sha256,
        str(PROJECTION_SCHEMA_VERSION), str(PROJECTION_RENDERER_VERSION),
    )).encode("utf-8")).hexdigest()[:32]
    meta = _metadata(plan, generation_id)
    ready_meta = {**meta, "status": "ready"}
    directory_fd = None
    owned_temp = owned_final = False
    temp = final = None
    try:
        parent = Path(directory).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        final = parent / ("gen-" + generation_id + ".sqlite")
        temp = parent / ("." + final.name + ".tmp-" + str(os.getpid()))
        if os.path.lexists(final):
            if final.is_symlink() or not final.is_file():
                raise ValueError("existing generation is not a regular file")
            _verify(final, plan, rows, active, ready_meta)
        else:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            owned_temp = True
            os.close(fd)
            _write_records(temp, plan, rows, active, meta)
            _verify(temp, plan, rows, active, meta)
            _sync_file(temp)
            os.fsync(directory_fd)
            os.replace(temp, final)
            owned_temp = False
            owned_final = True
            _verify(final, plan, rows, active, meta)
            with closing(sqlite3.connect(final)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                with connection:
                    connection.execute("INSERT INTO generation_meta VALUES ('status', 'ready')")
            _verify(final, plan, rows, active, ready_meta)
            _sync_file(final)
            os.fsync(directory_fd)
        return BuiltProjectionGeneration(str(final), generation_id, plan.commit_oid,
                                         plan.render_sha256, plan.total_records, True)
    except (OSError, sqlite3.Error, OverflowError) as exc:
        raise ValueError("cannot build/verify projection generation: " + str(exc)) from exc
    finally:
        # On failure, remove only this attempt's artifacts. Existing temp names
        # and conflicting final generations always remain untouched.
        import sys
        failed = sys.exc_info()[0] is not None
        try:
            if failed:
                for path, owned in ((temp, owned_temp), (final, owned_final)):
                    if owned:
                        for suffix in ("-journal", "-wal", "-shm", ""):
                            Path(str(path) + suffix).unlink(missing_ok=True)
                if directory_fd is not None and (owned_temp or owned_final):
                    os.fsync(directory_fd)
        except OSError as exc:
            raise ValueError("cannot clean failed projection generation: " + str(exc)) from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
