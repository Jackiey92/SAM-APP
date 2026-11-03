"""Offline, read-only verification of an isolated F-001 package.

R replays a packaged canonical closure record against fixed package bytes.
Old v2 packages without that record retain closure_replay_deferred. Declared E coverage additionally checks consumption rows and their full host
chain; optional legacy evidence never proves non-consumption. No
collector, source store, recovery action or host operation runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any

from app.facts_tree.backup_manifest import compute_core_digest, verify_final_manifest
from app.facts_tree.backup_assembly import (
    ASSEMBLY_CORE_SCHEMA, BUNDLE_NAME, MANIFEST_NAME, REPORT_NAME, PAYLOAD_SCHEMA, PAYLOAD_VERSION,
    _SAFE_ROLE_RE, _payload_probe,
    _replay_closure_digest, _replay_closure_bytes,
)
from app.facts_tree.backup_object_closure import _clean_relative_coordinate
from app.facts_tree.mapping_version_index import (
    _row_digest, _COLUMNS, _DB_FILENAME, SCHEMA, SCHEMA_VERSION,
    OBJECT_TYPE_MAPPING_VERSION, STORAGE_CLASS_MAPPING_OBJECTS, DIGEST_ALGORITHM,
)

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PATH = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
_HEX = frozenset("0123456789abcdef")
_GIT_COMMANDS = frozenset({
    ("init", "--bare"), ("bundle", "verify"), ("bundle", "list-heads"),
    ("bundle", "unbundle"), ("rev-parse",), ("rev-list",),
    ("cat-file",), ("ls-tree",), ("fsck",),
})
_HOST = ("host_approval_decisions", "host_prepared_effects", "host_task_credentials")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryVerdict:
    recoverable: bool
    package_format: int
    self_contained: bool
    backup_id: str
    barrier_id: str
    identity: dict
    bundle_head_oid: str
    object_format: str
    checks: tuple[CheckResult, ...]
    blockers: tuple[str, ...]
    advisories: tuple[str, ...]
    verified_at: str
    closure_r_list_digest: str | None

    @property
    def approval_audit(self):
        """Deep-immutable E report, default () means NOT EVALUATED.

        Intentionally not a dataclass field: the positional constructor and
        dataclasses.asdict/JSON key set remain byte-contract compatible.
        """
        return getattr(self, "_approval_audit", ())


def _hex(value, length):
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def _digest(value):
    return isinstance(value, str) and value.startswith("sha256:") and _hex(value[7:], 64)


def _integer(value):
    return type(value) is int and value >= 0


def _root(path):
    # Pin every ancestor too; a symlink in the supplied package path is refused.
    path = os.path.abspath(os.fspath(path))
    fd = os.open("/", _DIR)
    try:
        for segment in path.split("/")[1:]:
            if segment:
                child = os.open(segment, _DIR, dir_fd=fd)
                os.close(fd)
                fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(root, coordinate):
    parts = coordinate.split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError("unsafe coordinate")
    parent = os.open(".", _DIR, dir_fd=root)
    try:
        for segment in parts[:-1]:
            child = os.open(segment, _DIR, dir_fd=parent)
            os.close(parent)
            parent = child
        pinned = os.open(parts[-1], _PATH, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(pinned).st_mode):
                raise ValueError("not a regular file")
            return os.open(f"/proc/self/fd/{pinned}", os.O_RDONLY | os.O_CLOEXEC)
        finally:
            os.close(pinned)
    finally:
        os.close(parent)


def _bytes(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        part = os.read(fd, 1024 * 1024)
        if not part:
            return b"".join(chunks)
        chunks.append(part)


def _hash(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        part = os.read(fd, 1024 * 1024)
        if not part:
            return "sha256:" + digest.hexdigest(), size
        digest.update(part)
        size += len(part)


def _json(fd):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(_bytes(fd), object_pairs_hook=pairs)


def _sqlite(fd):
    return sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro&immutable=1", uri=True)


def _physical(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("integrity_check")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("foreign_key_check")
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")
        if not row[0].startswith("sqlite_")}


def _git(args, cwd, *, bundle_fd=None):
    assert any(tuple(args[:len(prefix)]) == prefix for prefix in _GIT_COMMANDS)
    def oid(value):
        return _hex(value, 40) or _hex(value, 64)
    command = args[0]
    if command == "bundle":
        assert len(args) == 3 and args[2] == "-" and bundle_fd is not None
    elif command == "init":
        assert (len(args) == 5 and args[2] in {"--object-format=sha1", "--object-format=sha256"}
                and args[3] == "--template=" and os.path.isabs(args[4])
                and os.path.commonpath((cwd, args[4])) == cwd and args[4] != cwd
                and os.path.basename(cwd).startswith("sam-recovery-git-"))
    elif command == "cat-file":
        assert (len(args) == 3 and ((args[1] == "-e" and args[2].endswith("^{commit}")
                and oid(args[2][:-9])) or (args[1] == "blob"
                and args[2].endswith(":meta/repository.json") and oid(args[2].split(":")[0]))))
    elif command == "fsck":
        assert len(args) == 4 and args[1:3] == ["--full", "--no-reflogs"] and oid(args[3])
    elif command == "rev-list":
        assert ((len(args) == 2 and oid(args[1])) or (len(args) == 3
                and args[1] in {"--parents", "--max-parents=0"} and oid(args[2])))
    else:
        # Reserved read-only commands; R4 currently needs neither of them.
        assert len(args) == 2 and oid(args[1])
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_") and "proxy" not in key.lower()
           and key not in {"HOME", "XDG_CONFIG_HOME"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0="/dev/null", GIT_CONFIG_KEY_1="protocol.allow",
               GIT_CONFIG_VALUE_1="never", HOME=cwd, XDG_CONFIG_HOME=cwd)
    if bundle_fd is not None:
        os.lseek(bundle_fd, 0, os.SEEK_SET)
    result = subprocess.run(["git", *args], cwd=cwd, env=env,
                            stdin=bundle_fd if bundle_fd is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30, check=True)
    return result.stdout.decode("utf-8", errors="strict").strip()


def _bundle(fd, core, fail):
    authority, block = core["authority"], core["bundle"]
    fmt = authority["object_format"]
    oid = authority["commit_oid"]
    if fmt not in {"sha1", "sha256"} or not _hex(oid, 40 if fmt == "sha1" else 64):
        fail("bundle_head_mismatch")
        return
    if (block.get("file") != BUNDLE_NAME or authority.get("ref") != "refs/heads/facts"
            or not _integer(authority.get("commit_count")) or authority["commit_count"] < 1):
        fail("bundle_authority_mismatch")
    if _hash(fd) != (block["sha256_digest"], block["size_bytes"]):
        fail("bundle_byte_mismatch")
        return
    pin = f"refs/sam/maintenance/backup-pins/{core['backup_id']}/{core['barrier_id']}"
    if authority["pin_ref"] != pin:
        fail("bundle_authority_mismatch")
        return
    # Stream the pinned bytes to git: this also works across procfs namespaces.
    bundle = "-"
    with tempfile.TemporaryDirectory(prefix="sam-recovery-git-") as scratch:
        bare = os.path.join(scratch, "authority.git")
        assert os.path.commonpath((scratch, bare)) == scratch and bare != scratch
        _git(["init", "--bare", f"--object-format={fmt}", "--template=", bare], scratch)
        _git(["bundle", "verify", bundle], bare, bundle_fd=fd)
        heads = _git(["bundle", "list-heads", bundle], bare, bundle_fd=fd).splitlines()
        if (heads != [f"{oid} {pin}"] or block["single_pin_ref"] is not True
                or block["advertised_refs"] != [pin]):
            fail("bundle_head_mismatch")
            return
        _git(["bundle", "unbundle", bundle], bare, bundle_fd=fd)
        _git(["cat-file", "-e", oid + "^{commit}"], bare)
        _git(["fsck", "--full", "--no-reflogs", oid], bare)
        commits = _git(["rev-list", oid], bare).splitlines()
        parents = _git(["rev-list", "--parents", oid], bare).splitlines()
        if any(len(line.split()) > 2 for line in parents):
            fail("bundle_authority_mismatch")
        expected_meta = {"schema_version": 1, "format_version": 1,
                         **{key: core["identity"][key] for key in
                            ("repo_id", "installation_id", "company_id")}}
        for commit in commits:
            meta = json.loads(_git(["cat-file", "blob", commit + ":meta/repository.json"], bare))
            if meta != expected_meta:
                fail("bundle_identity_mismatch")
        roots = _git(["rev-list", "--max-parents=0", oid], bare).splitlines()
        if len(commits) != authority["commit_count"] or roots != [authority["genesis_oid"]]:
            fail("bundle_authority_mismatch")


def _inventory(root, core, held, fail):
    block = core.get("payload")
    if not isinstance(block, dict):
        fail("payload_missing")
        return []
    if block.get("schema") != PAYLOAD_SCHEMA or (type(block.get("version")) is not int or block["version"] != PAYLOAD_VERSION):
        raise ValueError("payload schema")
    files = block["files"]
    paths, expected_dirs = set(), {"payload", "payload/w", "payload/r",
                                      "payload/r/archive", "payload/r/mapping_objects"}
    for entry in files:
        path, family = entry["path"], entry["family"]
        size, digest = entry["size_bytes"], entry["sha256_digest"]
        if not _integer(size) or not _digest(digest) or path in paths:
            raise ValueError("inventory fields")
        parts = path.split("/")
        if _clean_relative_coordinate(path, "payload") != path:
            fail("r_unsafe_coordinate")
            raise ValueError("coordinate")
        if family == "w":
            role = entry["role"]
            valid = bool(_SAFE_ROLE_RE.fullmatch(role)) and parts == ["payload", "w", role + ".sqlite"]
        elif family in {"r", "r2"}:
            if family == "r2" and path == "payload/" + _DB_FILENAME:
                valid = True
            else:
                cls = entry.get("storage_class")
                valid = (cls in {"archive", "mapping_objects"}
                         and parts == ["payload", "r", cls, digest[7:9], digest[7:]])
                if family == "r":
                    ids = entry["logical_ids"]
                    valid = valid and isinstance(ids, list) and bool(ids) and all(
                        isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids))
                else:
                    valid = valid and cls == "mapping_objects" and entry.get("ledger_backing") is True
        else:
            valid = False
        if not valid:
            fail("r_unsafe_coordinate" if family == "r" else "payload_malformed")
            raise ValueError("inventory coordinate")
        paths.add(path)
        expected_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    actual, dirs = set(), set()

    def walk(parent, prefix):
        for name in os.listdir(parent):
            rel = prefix + "/" + name if prefix else name
            info = _payload_probe(parent, name)
            if info is None:
                fail("payload_unsafe_path")
                continue
            if stat.S_ISDIR(info.st_mode):
                dirs.add(rel)
                if rel not in expected_dirs:
                    fail("payload_inventory_mismatch")
                    continue
                child = os.open(name, _DIR, dir_fd=parent)
                try:
                    walk(child, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                actual.add(rel)
                if rel in paths:
                    held[rel] = _file(root, rel)
            else:
                fail("payload_unsafe_path")
                fail("r_unsafe_coordinate")
                fail("payload_inventory_mismatch")
    try:
        payload = os.open("payload", _DIR, dir_fd=root)
    except OSError:
        fail("payload_missing")
        fail("payload_inventory_mismatch")
        return files
    dirs.add("payload")
    try:
        walk(payload, "payload")
    finally:
        os.close(payload)
    if actual != paths or dirs != expected_dirs:
        fail("payload_inventory_mismatch")
    if block["totals"] != {"files": len(files), "total_bytes": sum(e["size_bytes"] for e in files)}:
        fail("payload_inventory_mismatch")
    for entry in files:
        path = entry["path"]
        if path not in held:
            if path == "payload/" + _DB_FILENAME:
                fail("r2_missing")
                fail("payload_r2_missing")
            continue
        if _hash(held[path]) != (entry["sha256_digest"], entry["size_bytes"]):
            family = entry["family"]
            reason = ("w_byte_mismatch:" + entry["role"] if family == "w"
                      else "r2_byte_mismatch" if path == "payload/" + _DB_FILENAME
                      else "r_byte_mismatch")
            fail(reason)
            fail("payload_inventory_mismatch")
    return files


def _work(core, files, held, fail, host_dbs, approval_dbs=None):
    entries = [e for e in files if e["family"] == "w"]
    databases = core["databases"]
    anchors = {e["role"]: e for e in databases}
    roles = sorted(e["role"] for e in entries)
    if (len(anchors) != len(databases) or sorted(anchors) != roles
            or core["payload"]["w"] != {"roles": roles, "files": len(entries),
                "total_bytes": sum(e["size_bytes"] for e in entries)}):
        fail("payload_inventory_mismatch")
    for entry in entries:
        role, path = entry["role"], entry["path"]
        anchor = anchors.get(role, {})
        if (anchor.get("file") != role + ".sqlite"
                or (anchor.get("sha256_digest"), anchor.get("size_bytes"))
                != (entry["sha256_digest"], entry["size_bytes"])):
            fail("w_byte_mismatch:" + role)
        if path not in held:
            continue
        db = _sqlite(held[path])
        try:
            tables = _physical(db)
            declared = anchor.get("schema", {}).get("tables")
            if not isinstance(declared, list) or tables != set(declared):
                fail("w_schema_mismatch:" + role)
            host_dbs.append((db, tables))
            if approval_dbs is not None:
                approval_dbs.append((role, db, tables))
        except (sqlite3.Error, ValueError, TypeError):
            db.close()
            fail("w_corrupt:" + role)
        except Exception:
            db.close()
            raise


def _objects(core, files, held, fail):
    entries = [e for e in files if e["family"] == "r"]
    block, closure = core["payload"]["r"], core["closure"]
    derived = {"present_entries": sum(len(e["logical_ids"]) for e in entries),
               "physical_files": len(entries),
               "total_bytes": sum(e["size_bytes"] for e in entries), "by_class": {},
               "digests": sorted(_hash(held[e["path"]])[0] for e in entries if e["path"] in held)}
    for cls in ("archive", "mapping_objects"):
        selected = [e for e in entries if e["storage_class"] == cls]
        derived["by_class"][cls] = {"entries": sum(len(e["logical_ids"]) for e in selected),
            "files": len(selected), "bytes": sum(e["size_bytes"] for e in selected)}
    if (any(block.get(k) != v for k, v in derived.items())
            or len(set(derived["digests"])) != len(entries)):
        fail("r_inventory_mismatch")
    if not _digest(closure.get("r_list_digest")) or block.get("r_list_digest") != closure.get("r_list_digest"):
        fail("r_list_digest_mismatch")
    counts = closure["counts"]
    if (closure["status"] != "complete" or closure["publishable"] is not True
            or any(counts.get(k) != 0 for k in ("missing", "corrupt", "out_of_scope", "unsafe_path"))
            or counts.get("present") != derived["present_entries"]):
        fail("closure_not_publishable")


def _ledger(core, files, held, fail):
    block, anchor = core["payload"]["r2"], core["mapping_ledger"]
    entries = [e for e in files if e["family"] == "r2"]
    if block["status"] == "skipped":
        if entries or anchor is not None:
            fail("payload_r2_shape_mismatch")
        return
    if block["status"] != "ok":
        raise ValueError("ledger status")
    path = "payload/" + _DB_FILENAME
    if path not in held:
        fail("r2_missing")
        fail("payload_r2_missing")
        return
    if _hash(held[path]) != (block["ledger_file_sha256"], block["ledger_file_size"]):
        fail("r2_byte_mismatch")
    if block["backing_files"] != len(entries) - 1:
        fail("payload_r2_shape_mismatch")
    db = _sqlite(held[path])
    try:
        _physical(db)
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            fail("payload_r2_shape_mismatch")
        meta = db.execute("SELECT singleton,installation_id,company_id FROM fact_mapping_version_index_meta").fetchall()
        expected_meta = [(1, core["identity"]["installation_id"], core["identity"]["company_id"])]
        if meta != expected_meta:
            fail("r2_row_integrity")
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT " + ",".join(_COLUMNS) +
            " FROM fact_mapping_version_registrations ORDER BY logical_source_id, version_no")]
    finally:
        db.close()
    identity = core["identity"]
    registrations, sources = [], {}
    for row in rows:
        checksum = row.pop("row_sha256")
        if (checksum != _row_digest(**row) or not _hex(row["digest"], 64)
                or row["object_type"] != OBJECT_TYPE_MAPPING_VERSION
                or row["storage_class"] != STORAGE_CLASS_MAPPING_OBJECTS
                or row["source_kind"] not in {"online", "backfill"}
                or not _integer(row["version_no"]) or row["version_no"] < 1
                or not _integer(row["size_bytes"])
                or not isinstance(row["logical_source_id"], str) or not row["logical_source_id"]
                or any(row[k] != identity[k] for k in ("installation_id", "company_id"))):
            fail("r2_row_integrity")
        source = row["logical_source_id"]
        record = sources.setdefault(source, {"logical_source_id": source, "versions": 0,
                                            "gaps": 0, "gap_versions": []})
        record["versions"] += 1
        if row["version_no"] != record["versions"]:
            fail("payload_r2_shape_mismatch")
        backing = f"payload/r/mapping_objects/{row['digest'][:2]}/{row['digest']}"
        if backing not in held or _hash(held[backing]) != ("sha256:" + row["digest"], row["size_bytes"]):
            record["gaps"] += 1
            record["gap_versions"].append(row["version_no"])
            fail("r2_backing_missing")
        registrations.append({"logical_source_id": source, "version_no": row["version_no"],
            "sha256_digest": DIGEST_ALGORITHM + ":" + row["digest"], "size_bytes": row["size_bytes"],
            "source_kind": row["source_kind"], "source_coordinate": row["source_coordinate"],
            "h_commit_oid": row["h_commit_oid"]})
    payload = {"schema": SCHEMA, "version": SCHEMA_VERSION,
        "scope": {"kind": "company", "installation_id": identity["installation_id"],
                  "company_id": identity["company_id"]}, "object_type": OBJECT_TYPE_MAPPING_VERSION,
        "storage_class": STORAGE_CLASS_MAPPING_OBJECTS, "digest_algorithm": DIGEST_ALGORITHM,
        "registrations": registrations}
    if compute_core_digest(payload) != block["ledger_digest"] or block["ledger_digest"] != anchor["ledger_digest"]:
        fail("r2_ledger_digest_mismatch")
    shape = {"sources": list(sources.values()), "total_sources": len(sources),
             "total_versions": len(rows), "total_gaps": sum(s["gaps"] for s in sources.values())}
    if any(block.get(k) != v or anchor.get(k) != v for k, v in shape.items()):
        fail("payload_r2_shape_mismatch")


class _JsonDecimal(str):
    """Keep exact JSON fractional text; never round it through a float."""


def _host_json(blob):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate host key")
            value[key] = item
        return value

    def reject(_value):
        raise ValueError("nonfinite host number")

    return json.loads(blob, object_pairs_hook=pairs, parse_float=_JsonDecimal,
                      parse_constant=reject)


def _host_bytes(payload):
    # Same two-space, sorted, newline-terminated record format as the host.
    # Decimal tokens in canonical records are fixed-point, preserving scale.
    def render(value, depth=0):
        if depth > 32:
            raise ValueError("host nesting")
        if isinstance(value, _JsonDecimal):
            if "e" in value.lower():
                raise ValueError("noncanonical decimal")
            return str(value)
        if isinstance(value, dict):
            if not value:
                return "{}"
            pad = "  " * (depth + 1)
            return "{\n" + ",\n".join(pad + json.dumps(k, ensure_ascii=False) + ": " +
                render(v, depth + 1) for k, v in sorted(value.items())) + "\n" + "  " * depth + "}"
        if isinstance(value, list):
            if not value:
                return "[]"
            pad = "  " * (depth + 1)
            return "[\n" + ",\n".join(pad + render(v, depth + 1) for v in value) + "\n" + "  " * depth + "]"
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return (render(payload) + "\n").encode("utf-8")


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _host(databases, identity, fail, advise):
    found, decisions, reservations, operations, prepared = set(), {}, set(), set(), []
    referenced_tasks, credential_tasks = set(), set()
    for db, tables in databases:
        db.row_factory = sqlite3.Row
        found.update(tables.intersection(_HOST))
        for table in tables.intersection(set(_HOST) | {"host_approval_reservations"}):
            triggers = [r[0].upper() for r in db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)) if r[0]]
            if any(not any("RAISE(ABORT" in sql.replace(" ", "") and
                           ("BEFORE" + action) in sql.replace(" ", "") for sql in triggers)
                   for action in ("UPDATE", "DELETE")):
                fail("host_guard_missing:" + table)
            if table == "host_task_credentials":
                credential_tasks.update(r[0] for r in db.execute("SELECT task_id FROM host_task_credentials"))
                _credentials(db, tables, identity, fail, advise)
                continue
            domain = {"host_approval_decisions": b"sam-host-approval-decision row-v1\x00",
                      "host_prepared_effects": b"sam-host-prepared-effect row-v1\x00",
                      "host_approval_reservations": b"sam-host-approval-reservation row-v1\x00"}[table]
            for raw in db.execute("SELECT * FROM " + _quote(table)):
                row = dict(raw)
                referenced_tasks.add(row["task_id"])
                blob = bytes(row["record_blob"])
                payload = _host_json(blob)
                canonical = _host_bytes(payload)
                if (hashlib.sha256(domain + blob).hexdigest() != row["row_sha256"]
                        or canonical != blob or any(payload.get(k) != v for k, v in row.items()
                            if k not in {"record_blob", "row_sha256"})):
                    fail("host_row_integrity:" + table)
                if any(payload.get(k) != identity[k] for k in ("installation_id", "company_id")):
                    fail("host_scope_mismatch:" + table)
                if (payload.get("schema_version") != 1
                        or any(not _hex(payload.get(k), 64) for k in ("effect_digest", "preparation_digest"))
                        or type(payload.get("created_at")) is not str
                        or type(payload.get("expires_at")) is not str
                        or payload["created_at"] >= payload["expires_at"]):
                    fail("host_row_integrity:" + table)
                op = row["operation_id"]
                if table == "host_approval_decisions":
                    if row["decision"] not in {"approved", "denied"} or op in decisions:
                        fail("host_decision_conflict:" + op)
                    decisions[op] = row["decision"]
                    advise("host_decided_operation:" + op + ":" + row["decision"])
                elif table == "host_approval_reservations":
                    reservations.add(op)
                else:
                    if payload.get("preparation_state") != "prepared":
                        fail("host_row_integrity:" + table)
                    for keys, domain_name, target in (
                        (("action", "target", "objects", "effect_manifest", "object_format", "base_commit", "dependency_versions"),
                         b"sam-host-prepared-effect effect-v1\x00", "effect_digest"),
                        (("schema_version", "installation_id", "company_id", "principal_id", "member_revision", "task_id", "run_id", "policy_id", "policy_revision", "idempotency_key", "expires_at", "effect_digest"),
                         b"sam-host-prepared-effect preparation-v1\x00", "preparation_digest")):
                        data = _host_bytes({k: payload[k] for k in keys})
                        if hashlib.sha256(domain_name + data).hexdigest() != payload[target]:
                            fail("host_row_integrity:" + table)
                    prepared.append(op)
        for table in tables:
            if table.startswith("fact_operation_"):
                columns = {r[1] for r in db.execute("PRAGMA table_info(" + _quote(table) + ")")}
                if {"operation_id", "company_id", "repo_id"} <= columns:
                    operations.update(r[0] for r in db.execute(
                        'SELECT operation_id FROM ' + _quote(table) +
                        ' WHERE company_id=? AND repo_id=?',
                        (identity["company_id"], identity["repo_id"])))
    for task in sorted(referenced_tasks - credential_tasks):
        fail("credential_state_unknown:" + task)
    if not found:
        advise("host_tables_absent")
    for op in sorted(reservations - decisions.keys()):
        advise("host_pending_operation:" + op)
    for op in prepared:
        if op not in decisions and op not in reservations and op not in operations:
            reason = "host_unknown_operation:" + op
            fail(reason)
            advise(reason)


def _credentials(db, tables, identity, fail, advise):
    credentials = [dict(r) for r in db.execute("SELECT * FROM host_task_credentials")]
    tasks = {row["task_id"] for row in credentials}
    for row in credentials:
        if (any(row[k] != identity[k] for k in ("installation_id", "company_id"))
                or not _hex(row["credential_sha256"], 64)):
            fail("credential_state_unknown:" + row["task_id"])
    if not {"host_task_credential_events", "host_task_credentials_ledger_state"} <= tables:
        for task in tasks:
            fail("credential_state_unknown:" + task)
        return
    required_guards = {
        "host_task_credential_events": ("host_task_credential_events_no_update",
            "host_task_credential_events_no_delete", "host_task_credential_events_no_replace",
            "host_task_credential_events_issued_once", "host_task_credential_events_revoked_needs_issued",
            "host_task_credential_events_revoked_once"),
        "host_task_credentials_ledger_state": ("host_task_credentials_seq_increment_only",
            "host_task_credentials_seq_no_delete", "host_task_credentials_seq_no_replace"),
    }
    for table, names in required_guards.items():
        triggers = {r[0]: r[1] for r in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if any(name not in triggers or "RAISE(ABORT" not in "".join(triggers[name].upper().split())
               for name in names):
            fail("host_guard_missing:" + table)
    events = [dict(r) for r in db.execute(
        "SELECT * FROM host_task_credential_events ORDER BY task_id,event_seq")]
    state = db.execute("SELECT singleton,revocation_seq FROM host_task_credentials_ledger_state").fetchall()
    revoked = sorted(e["revocation_seq"] for e in events if e["event"] == "revoked")
    water_ok = (len(state) == 1 and state[0][0] == 1 and _integer(state[0][1])
                and len(revoked) == state[0][1]
                and all(value == index for index, value in enumerate(revoked, 1)))
    if any(e["task_id"] not in tasks for e in events):
        fail("credential_event_orphan")
    for task in sorted(tasks):
        own = [e for e in events if e["task_id"] == task]
        valid = (water_ok and [e["event"] for e in own] in
                 (["issued"], ["issued", "revoked"])
                 and [e["event_seq"] for e in own] == list(range(1, len(own) + 1))
                 and own[0]["revocation_seq"] == 0
                 and all(type(e["changed_at"]) is str for e in own)
                 and [e["changed_at"] for e in own] == sorted(e["changed_at"] for e in own))
        if not valid:
            fail("credential_state_unknown:" + task)
        elif own[-1]["event"] == "revoked":
            advise("credential_revoked:" + task)
        else:
            # A different task's later revocation does not revoke this task.
            advise("credential_rebind_required:" + task)


# E capability is explicitly versioned inside the digest-bound manifest core.
# This is a declaration of backup coverage, never current ledger authority.
_APPROVAL_CAPABILITY = "approval_consumptions"
_APPROVAL_TABLES = (
    "host_prepared_effects", "host_approval_decisions",
    "host_approval_reservations", "host_approval_consumptions",
)
_APPROVAL_COMMON = frozenset({
    "schema_version", "operation_id", "installation_id", "company_id",
    "principal_id", "member_revision", "task_id", "run_id", "policy_id",
    "policy_revision", "effect_digest", "preparation_digest", "created_at", "expires_at",
})
_APPROVAL_SHAPES = {
    "host_prepared_effects": _APPROVAL_COMMON | {
        "preparation_state", "idempotency_key", "action", "target", "objects",
        "effect_manifest", "object_format", "base_commit", "dependency_versions"},
    "host_approval_decisions": _APPROVAL_COMMON | {"approval_id", "session_ref", "decision"},
    "host_approval_reservations": _APPROVAL_COMMON | {
        "reservation_state", "reservation_id", "approval_id", "action", "target"},
    "host_approval_consumptions": _APPROVAL_COMMON | {
        "consumption_state", "reservation_id", "approval_id", "action", "target", "reservation_created_at"},
}
_APPROVAL_COLUMNS = {
    "host_prepared_effects": ("operation_id", "task_id", "run_id", "idempotency_key",
        "effect_digest", "preparation_digest", "created_at", "expires_at", "record_blob", "row_sha256"),
    "host_approval_decisions": ("approval_id", "operation_id", "task_id", "run_id",
        "effect_digest", "preparation_digest", "decision", "created_at", "expires_at", "record_blob", "row_sha256"),
    "host_approval_reservations": ("reservation_id", "approval_id", "operation_id", "task_id", "run_id",
        "effect_digest", "preparation_digest", "created_at", "expires_at", "record_blob", "row_sha256"),
    "host_approval_consumptions": ("reservation_id", "approval_id", "operation_id", "task_id", "run_id",
        "effect_digest", "preparation_digest", "created_at", "expires_at", "record_blob", "row_sha256"),
}
_APPROVAL_UNIQUE = {
    "host_prepared_effects": {("operation_id",), ("task_id", "run_id", "idempotency_key")},
    "host_approval_decisions": {("approval_id",), ("operation_id",)},
    "host_approval_reservations": {("reservation_id",), ("approval_id",), ("operation_id",)},
    "host_approval_consumptions": {("reservation_id",), ("approval_id",), ("operation_id",)},
}


def _approval_freeze(value):
    """New verdict data uses only immutable tuples and scalar JSON values."""
    if isinstance(value, dict):
        return tuple((key, _approval_freeze(item)) for key, item in sorted(value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_approval_freeze(item) for item in value)
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("approval data type")


def _approval_id(value):
    # Same UTF-8/character/encoded-byte limits as git_format.encode_id.
    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("approval id")
    if sum(1 if chr(b).isascii() and (chr(b).isalnum() or chr(b) in "_-") else 3
           for b in value.encode("utf-8")) > 384:
        raise ValueError("approval id")


def _approval_time(db, value):
    if (type(value) is not str or len(value) != 32 or value[19] != "."
            or value[26:] != "+00:00" or not value[20:26].isascii()
            or not value[20:26].isdigit() or value[:4] == "0000"):
        raise ValueError("approval time")
    normal = db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S', ?, '+0 seconds')", (value,)).fetchone()[0]
    if normal != value[:19]:
        raise ValueError("approval time")
    return value


def _approval_sql(sql):
    return "".join(sql.upper().split()).replace("IFNOTEXISTS", "").rstrip(";")


def _approval_storage(db, table):
    """Check physical tables, all unique indexes and exact v1 conflict guards.

    Guard SQL below is compared as DATA, never executed; no service, scratch
    database, DDL or repair is used to inspect a hostile backup.
    """
    objects = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchall()
    if len(objects) != 1 or objects[0][0] != "table":
        raise ValueError("approval table")
    columns = db.execute("PRAGMA table_info(" + _quote(table) + ")").fetchall()
    if tuple(r[1] for r in columns) != _APPROVAL_COLUMNS[table]:
        raise ValueError("approval columns")
    if any(r[2].upper() != ("BLOB" if r[1] == "record_blob" else "TEXT") for r in columns):
        raise ValueError("approval types")
    for position,column in enumerate(columns):
        expected_null = 1 if table == "host_approval_consumptions" or position else 0
        if column[3] != expected_null or column[4] is not None or column[5] != (1 if position == 0 else 0):
            raise ValueError("approval column constraints")
    observed = set()
    for index in db.execute("PRAGMA index_list(" + _quote(table) + ")"):
        if index[2] and not index[4]:
            observed.add(tuple(r[2] for r in db.execute("PRAGMA index_info(" + _quote(index[1]) + ")")))
    if observed != _APPROVAL_UNIQUE[table]:
        raise ValueError("approval unique")
    noun = {"host_prepared_effects": "host prepared effects",
            "host_approval_decisions": "host approval decisions",
            "host_approval_reservations": "host approval reservations"}.get(table)
    message = noun + " are append-only" if noun else "approval_consumption_append_only"
    expected = {}
    for action in ("update", "delete"):
        name = table + "_no_" + action
        expected[name] = ("CREATE TRIGGER " + name + " BEFORE " + action + " ON " + table
                          + " BEGIN SELECT RAISE(ABORT, '" + message + "'); END")
    if table == "host_approval_consumptions":
        name = table + "_no_replace"
        condition = " OR ".join(key + " = NEW." + key for key in ("reservation_id", "approval_id", "operation_id"))
        expected[name] = ("CREATE TRIGGER " + name + " BEFORE INSERT ON " + table
            + " WHEN EXISTS (SELECT 1 FROM " + table + " WHERE " + condition
            + ") BEGIN SELECT RAISE(ABORT, '" + message + "'); END")
    else:
        conflicts = {
            "host_prepared_effects": (
                ("operation", "operation_id = NEW.operation_id", "an existing operation"),
                ("idempotency", "task_id = NEW.task_id AND run_id = NEW.run_id AND idempotency_key = NEW.idempotency_key", "an existing idempotency key")),
            "host_approval_decisions": (
                ("approval", "approval_id = NEW.approval_id", "an existing decision"),
                ("operation", "operation_id = NEW.operation_id", "an existing operation decision")),
            "host_approval_reservations": (
                ("reservation", "reservation_id = NEW.reservation_id", "an existing reservation"),
                ("approval", "approval_id = NEW.approval_id", "an existing approval reservation"),
                ("operation", "operation_id = NEW.operation_id", "an existing operation reservation")),
        }[table]
        for suffix, condition, target in conflicts:
            name = table + "_no_replace_" + suffix
            expected[name] = ("CREATE TRIGGER " + name + " BEFORE INSERT ON " + table
                + " WHEN EXISTS (SELECT 1 FROM " + table + " WHERE " + condition
                + ") BEGIN SELECT RAISE(ABORT, '" + message
                + ": INSERT OR REPLACE cannot overwrite " + target + "'); END")
    actual = {r[0]: _approval_sql(r[1]) for r in db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)) if r[1]}
    if actual != {name: _approval_sql(sql) for name, sql in expected.items()}:
        raise ValueError("approval guards")


def _approval_row(db, table, row, identity):
    blob = row["record_blob"]
    if not isinstance(blob, (bytes, bytearray, memoryview)) or not _hex(row["row_sha256"], 64):
        raise ValueError("approval bytes")
    blob = bytes(blob)
    payload = _host_json(blob)
    domain = {"host_prepared_effects": "prepared-effect", "host_approval_decisions": "approval-decision",
              "host_approval_reservations": "approval-reservation", "host_approval_consumptions": "approval-consumption"}[table]
    if (type(payload) is not dict or set(payload) != _APPROVAL_SHAPES[table]
            or _host_bytes(payload) != blob
            or hashlib.sha256(("sam-host-" + domain + " row-v1\0").encode() + blob).hexdigest() != row["row_sha256"]
            or any(payload.get(k) != v for k,v in row.items() if k not in {"record_blob", "row_sha256"})):
        raise ValueError("approval self proof")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("approval version")
    for key in ("operation_id", "installation_id", "company_id", "principal_id", "task_id", "run_id", "policy_id",
                *(k for k in ("reservation_id", "approval_id", "session_ref", "idempotency_key") if k in payload)):
        _approval_id(payload[key])
    for key in ("member_revision", "policy_revision"):
        if type(payload[key]) is not int or payload[key] < 1:
            raise ValueError("approval revision")
    if any(payload[k] != identity[k] for k in ("installation_id", "company_id")):
        raise ValueError("approval scope")
    if any(not _hex(payload[k], 64) for k in ("effect_digest", "preparation_digest")):
        raise ValueError("approval digest")
    if _approval_time(db, payload["created_at"]) >= _approval_time(db, payload["expires_at"]):
        raise ValueError("approval interval")
    if table == "host_approval_decisions" and payload["decision"] not in {"approved", "denied"}:
        raise ValueError("approval decision")
    if table == "host_approval_reservations" and payload["reservation_state"] != "reserved":
        raise ValueError("approval reservation")
    if table == "host_approval_consumptions":
        if (payload["consumption_state"] != "consumed" or
                _approval_time(db, payload["reservation_created_at"]) > payload["created_at"]):
            raise ValueError("approval consumption")
    if table != "host_approval_decisions":
        if any(type(payload[k]) is not str or not payload[k] or len(payload[k]) > 200 for k in ("action", "target")):
            raise ValueError("approval target")
    if table == "host_prepared_effects":
        objects = payload["objects"]
        deps = payload["dependency_versions"]
        if (payload["preparation_state"] != "prepared" or type(objects) is not list or not objects
                or any(type(v) is not str or not v or len(v) > 200 for v in objects)
                or objects != sorted(set(objects)) or type(payload["effect_manifest"]) is not dict
                or payload["object_format"] not in {"sha1", "sha256"}
                or not _hex(payload["base_commit"], 40 if payload["object_format"] == "sha1" else 64)
                or type(deps) is not dict or any(type(k) is not str or not k or len(k)>200
                    or type(v) is not str or not v or len(v)>200 for k,v in deps.items())):
            raise ValueError("approval preparation")
        for keys, domain, target in (
            (("action", "target", "objects", "effect_manifest", "object_format", "base_commit", "dependency_versions"),
             b"sam-host-prepared-effect effect-v1\0", "effect_digest"),
            (("schema_version", "installation_id", "company_id", "principal_id", "member_revision", "task_id", "run_id",
              "policy_id", "policy_revision", "idempotency_key", "expires_at", "effect_digest"),
             b"sam-host-prepared-effect preparation-v1\0", "preparation_digest")):
            if hashlib.sha256(domain + _host_bytes({k:payload[k] for k in keys})).hexdigest() != payload[target]:
                raise ValueError("approval preparation digest")
    return payload


def _approval_work(db, tables, identity):
    """Checksum-bound old work evidence, without constructing a writable store."""
    records, events = {}, {}
    if "fact_operation_work_records" in tables:
        for raw in db.execute("SELECT * FROM fact_operation_work_records"):
            row = dict(raw)
            blob = bytes(row["request_blob"])
            keys = ("repo_id", "company_id", "task_id", "operation_id", "idempotency_key", "created_at", "request_sha256")
            if (_host_bytes(_host_json(blob)) != blob or hashlib.sha256(blob).hexdigest() != row["request_sha256"]
                    or hashlib.sha256(_host_bytes({k:row[k] for k in keys})).hexdigest() != row["row_sha256"]
                    or any(row[k] != identity[k] for k in ("repo_id", "company_id"))):
                raise ValueError("approval work record")
            records[row["operation_id"]] = row["task_id"]
    if "fact_operation_work_events" in tables:
        for raw in db.execute("SELECT * FROM fact_operation_work_events ORDER BY seq"):
            row = dict(raw)
            blob = bytes(row["detail_blob"])
            columns = {k:v for k,v in row.items() if k not in {"detail_blob", "row_sha256"}}
            if (_host_bytes(_host_json(blob)) != blob or hashlib.sha256(blob).hexdigest() != row["detail_sha256"]
                    or hashlib.sha256(_host_bytes(columns)).hexdigest() != row["row_sha256"]
                    or any(row[k] != identity[k] for k in ("repo_id", "company_id"))
                    or row["operation_id"] not in records or type(row["seq"]) is not int or row["seq"] < 1):
                raise ValueError("approval work event")
            events.setdefault(row["operation_id"], []).append(row)
    unknown, origins = [], {}
    for op, rows in events.items():
        if [r["seq"] for r in rows] != list(range(1,len(rows)+1)):
            raise ValueError("approval work sequence")
        if any(r["state"] == "unknown" for r in rows) or rows[-1]["state"] == "in_flight":
            unknown.append(op)
            origins[op] = next((r["row_sha256"] for r in reversed(rows) if r["state"] == "unknown"), rows[-1]["row_sha256"])
    return records, unknown, origins


def _approval_snapshot(core, databases):
    """Read all independent hard facts, including from undeclared old W.

    A declaration makes errors mandatory R4 blockers. Its absence never proves
    non-consumption; valid old reservation/denial/revocation facts remain useful.
    Each row is isolated so damage in one path cannot erase another hard fact.
    """
    errors, rows, chains, consumed, revoked, unknown = set(), [], set(), set(), set(), set()
    declaration = core.get("recovery_capabilities", {})
    declared = type(declaration) is dict and _APPROVAL_CAPABILITY in declaration
    roles = ()
    if "recovery_capabilities" in core and type(declaration) is not dict:
        declared = True
        errors.add("approval_capability_invalid")
    if declared:
        cap = declaration.get(_APPROVAL_CAPABILITY) if type(declaration) is dict else None
        if (type(cap) is not dict or set(cap) != {"version", "roles"}
                or type(cap["version"]) is not int or cap["version"] != 1
                or type(cap["roles"]) is not list or not cap["roles"]
                or any(type(r) is not str or not _SAFE_ROLE_RE.fullmatch(r) for r in cap["roles"])
                or len(set(cap["roles"])) != len(cap["roles"])):
            errors.add("approval_capability_invalid")
        else:
            roles = tuple(sorted(cap["roles"]))
    actual_roles = {role for role, _, _ in databases}
    if not set(roles) <= actual_roles:
        errors.add("approval_consumption_carrier_missing")
    all_by_table = {t:{} for t in _APPROVAL_TABLES}
    unique_seen = {table:{keys:set() for keys in _APPROVAL_UNIQUE[table]} for table in _APPROVAL_TABLES}
    credentials = {}
    work_tasks, restrictive_rows = {}, []
    for role, db, tables in databases:
        db.row_factory = sqlite3.Row
        if role in roles and not set(_APPROVAL_TABLES) <= tables:
            errors.add("approval_consumption_carrier_missing")
        for table in _APPROVAL_TABLES:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
            if not exists:
                continue
            try:
                _approval_storage(db, table)
            except Exception:
                errors.add("approval_consumption_schema_invalid")
                continue
            try:
                raw_rows = db.execute("SELECT * FROM " + _quote(table)).fetchall()
            except Exception:
                errors.add("approval_consumption_schema_invalid")
                continue
            for raw in raw_rows:
                try:
                    raw = dict(raw)
                    payload = _approval_row(db, table, raw, core["identity"])
                    op = payload["operation_id"]
                    if any(tuple(payload[key] for key in keys) in seen
                           for keys,seen in unique_seen[table].items()):
                        errors.add("approval_consumption_duplicate")
                        continue
                    for keys,seen in unique_seen[table].items():
                        seen.add(tuple(payload[key] for key in keys))
                    all_by_table[table][op] = payload
                    # Secrets, policy bodies and effect manifests never enter
                    # the portable verdict report or its repr/JSON consumers.
                    public = {k:v for k,v in payload.items() if k not in {
                        "objects", "effect_manifest", "dependency_versions"}}
                    rows.append({"role":role, "table":table, "row_sha256":raw["row_sha256"],
                                 "record_sha256":hashlib.sha256(raw["record_blob"]).hexdigest(), "record":public})
                except Exception:
                    errors.add("approval_consumption_row_invalid")
        if "host_task_credentials" in tables:
            local = set()
            try:
                _credentials(db, tables, core["identity"], local.add, lambda _: None)
                if local:
                    raise ValueError("credential proof")
                for raw in db.execute("SELECT * FROM host_task_credentials"):
                    credential = dict(raw)
                    if credential["task_id"] in credentials:
                        raise ValueError("credential duplicate")
                    credentials[credential["task_id"]] = credential
                for raw in db.execute("SELECT * FROM host_task_credential_events WHERE event='revoked'"):
                    event = dict(raw)
                    revoked.add(event["task_id"])
                    restrictive_rows.append({"role":role, "table":"host_task_credential_events",
                        "task_id":event["task_id"], "operation_id":None, "state":"revoked",
                        "row_sha256":hashlib.sha256(_host_bytes(event)).hexdigest()})
            except Exception:
                errors.add("approval_credential_evidence_invalid")
        try:
            tasks, unclear, work_origins = _approval_work(db, tables, core["identity"])
            work_tasks.update(tasks)
            unknown.update(unclear)
            for op in unclear:
                restrictive_rows.append({"role":role, "table":"fact_operation_work_events",
                    "task_id":tasks[op], "operation_id":op, "state":"unknown",
                    "row_sha256":work_origins[op]})
        except Exception:
            errors.add("approval_work_evidence_invalid")
    prepared, decisions, reservations, consumptions = (all_by_table[t] for t in _APPROVAL_TABLES)
    for op, decision in decisions.items():
        try:
            effect = prepared[op]
            credential = credentials[decision["task_id"]]
            common = _APPROVAL_COMMON - {"created_at", "expires_at"}
            if (any(decision[k] != effect[k] for k in common)
                    or any(credential[k] != effect[k] for k in ("task_id", "run_id", "installation_id", "company_id",
                        "principal_id", "member_revision", "policy_id", "policy_revision"))
                    or not effect["created_at"] <= decision["created_at"] < decision["expires_at"] <= effect["expires_at"]):
                raise ValueError("approval chain")
            scope = _host_json(credential["scope_json"])
            if (effect["action"] not in scope["actions"] or effect["target"] != scope["target"]
                    or not set(effect["objects"]) <= set(scope["objects"])
                    or effect["expires_at"] > scope["not_after"]
                    or (op in work_tasks and work_tasks[op] != effect["task_id"])):
                raise ValueError("approval chain scope")
            chains.add(op)
        except Exception:
            errors.add("approval_consumption_chain_invalid")
    for op, reservation in reservations.items():
        try:
            decision, effect = decisions[op], prepared[op]
            if (op not in chains or decision["decision"] != "approved"
                    or any(reservation[k] != decision[k] for k in _APPROVAL_COMMON - {"created_at", "expires_at"})
                    or reservation["approval_id"] != decision["approval_id"]
                    or any(reservation[k] != effect[k] for k in ("action", "target"))
                    or not decision["created_at"] <= reservation["created_at"] < reservation["expires_at"] == decision["expires_at"]):
                raise ValueError("approval reservation chain")
        except Exception:
            errors.add("approval_consumption_chain_invalid")
            chains.discard(op)
    for op, consumption in consumptions.items():
        try:
            reservation = reservations[op]
            expected = dict(consumption)
            expected.pop("consumption_state")
            expected["reservation_state"] = "reserved"
            expected["created_at"] = expected.pop("reservation_created_at")
            if (op not in chains or expected != reservation
                    or not reservation["created_at"] <= consumption["created_at"] < consumption["expires_at"]):
                raise ValueError("approval consumed chain")
            consumed.add(op)
        except Exception:
            errors.add("approval_consumption_chain_invalid")
    sources = tuple((entry["role"], entry["path"], entry["sha256_digest"], entry["size_bytes"])
        for entry in core.get("payload", {}).get("files", ()) if entry.get("family") == "w")
    return _approval_freeze({"version":1, "declared":declared, "roles":roles,
        "core_digest":compute_core_digest(core), "sources":sources,
        "records":rows, "chain_operations":sorted(chains), "consumption_operations":sorted(consumed),
        "revoked_tasks":sorted(revoked), "unknown_operations":sorted(unknown),
        "restrictive_rows":restrictive_rows, "failures":sorted(errors)})


def verify_recovery_package(package_dir, *, expected_identity=None) -> RecoveryVerdict:
    """Collect independent read-only checks; malformed/unreadable input fails closed.

    Digests detect drift, not an external signature or authenticity of a supplied
    package. expected_identity is the optional caller-supplied identity anchor.
    """
    checks, blockers, advisories = [], set(), {"closure_replay_deferred"}
    held: dict[str, Any] = {}
    host_dbs, approval_dbs, approval_audit = [], [], ()
    root, core, version, self_contained = None, {}, 0, False
    # SQLite's UTC clock avoids importing a module outside the frozen whitelist.
    clock = sqlite3.connect("file::memory:?mode=ro", uri=True)
    try:
        verified_at = clock.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    finally:
        clock.close()

    def stage(name, action, error):
        reasons = set()
        try:
            action(reasons.add)
        except Exception:
            # Untrusted JSON, SQLite and git failures never escape the API.
            # Exception text may contain hostile paths or confidential records.
            reasons.add(error)
        blockers.update(reasons)
        checks.append(CheckResult(name, not reasons, tuple(sorted(reasons))))
        return not reasons

    try:
        def envelope(fail):
            nonlocal root, core, version
            root = _root(package_dir)
            for name in (BUNDLE_NAME, MANIFEST_NAME, REPORT_NAME):
                try:
                    held[name] = _file(root, name)
                except (OSError, ValueError):
                    fail("missing_manifest_or_report" if name != BUNDLE_NAME else "bundle_missing")
            if MANIFEST_NAME not in held or REPORT_NAME not in held:
                return
            try:
                manifest, report = _json(held[MANIFEST_NAME]), _json(held[REPORT_NAME])
            except ValueError as exc:
                if str(exc) == "duplicate JSON key":
                    fail("closure_record_malformed")
                raise
            version = manifest.get("version", 0)
            if type(version) is not int or version not in {1, 2}:
                fail("unsupported_envelope_version")
                version = 0
                return
            try:
                verify_final_manifest(manifest, report)
            except ValueError:
                # Some illegal JSON values (notably floating sizes) cannot
                # even be serialized into the digest chain. Preserve the
                # malformed-record reason as well as the envelope failure.
                candidate = manifest.get("core")
                if (isinstance(candidate, dict) and isinstance(candidate.get("closure"), dict)
                        and "canonical_record" in candidate["closure"]):
                    malformed = set()
                    try:
                        _replay_closure_digest(candidate, malformed.add)
                    except Exception:
                        malformed.add("closure_record_malformed")
                    if "closure_record_malformed" in malformed:
                        fail("closure_record_malformed")
                raise
            if not isinstance(manifest["core"], dict):
                raise ValueError("core")
            core = manifest["core"]
            if (core.get("schema") != ASSEMBLY_CORE_SCHEMA or type(core.get("version")) is not int or core["version"] != 1
                    or core.get("digest_algorithm") != "sha256"
                    or any(not isinstance(core.get(k), str) or not _SAFE_ROLE_RE.fullmatch(core[k])
                           for k in ("backup_id", "barrier_id"))):
                fail("package_malformed")
        envelope_ok = stage("envelope", envelope, "digest_chain")
        if core and envelope_ok:
            def identity(fail):
                value = core["identity"]
                if (not isinstance(value, dict) or any(not isinstance(value.get(k), str)
                    or not value[k].strip() for k in ("installation_id", "company_id", "repo_id"))
                    or ("scope" in value and value["scope"] != {"kind": "company",
                        "installation_id": value["installation_id"], "company_id": value["company_id"]})
                    or (expected_identity is not None and value != expected_identity)):
                    fail("identity_mismatch")
            stage("identity", identity, "identity_mismatch")
            if BUNDLE_NAME in held:
                stage("bundle", lambda fail: _bundle(held[BUNDLE_NAME], core, fail), "bundle_invalid")
            if version == 1:
                blockers.add("legacy_three_file")
                advisories.add("legacy_three_file_rebackup_required")
                checks.append(CheckResult("payload", False, ("legacy_three_file",)))
            else:
                files = []
                def inventory(fail):
                    nonlocal files
                    files = _inventory(root, core, held, fail)
                self_contained = stage("payload", inventory, "payload_malformed")
                # Continue after physical damage to collect useful W/R/R2 diagnostics.
                stage("W", lambda fail: _work(core, files, held, fail, host_dbs, approval_dbs), "payload_malformed")
                stage("R", lambda fail: _objects(core, files, held, fail), "payload_malformed")
                closure = core.get("closure")
                if isinstance(closure, dict) and "canonical_record" in closure:
                    digest_ok = stage("R.closure_digest", lambda fail: _replay_closure_digest(core, fail),
                                      "closure_record_malformed")
                    bytes_ok = stage("R.closure_bytes", lambda fail: _replay_closure_bytes(
                        core, files, lambda path: _hash(held[path]), fail), "closure_record_malformed")
                    if digest_ok and bytes_ok:
                        advisories.discard("closure_replay_deferred")
                stage("R2", lambda fail: _ledger(core, files, held, fail), "payload_r2_shape_mismatch")
                self_contained = self_contained and all(c.passed for c in checks if c.name in {
                    "W", "R", "R2", "R.closure_digest", "R.closure_bytes"})
                stage("host", lambda fail: _host(host_dbs, core["identity"], fail, advisories.add), "host_malformed")
                def approvals(fail):
                    nonlocal approval_audit
                    approval_audit = _approval_snapshot(core, approval_dbs)
                    audit = dict(approval_audit)
                    if audit["declared"]:
                        for code in audit["failures"]:
                            fail(code)
                # The independent row report survives a red legacy host check.
                # Mandatory E errors enter the same stage/blockers set as R4.
                if "recovery_capabilities" in core:
                    stage("E.approvals", approvals, "approval_consumption_unreadable")
                else:
                    try:
                        approvals(lambda _: None)
                    except Exception:
                        approval_audit = ()
    finally:
        for db, _tables in host_dbs:
            db.close()
        for fd in held.values():
            os.close(fd)
        if root is not None:
            os.close(root)
    identity_value = core.get("identity", {})
    if not isinstance(identity_value, dict):
        identity_value = {}
    authority = core.get("authority", {})
    closure = core.get("closure") or {}
    verdict = RecoveryVerdict(
        recoverable=not blockers and self_contained, package_format=version,
        self_contained=self_contained, backup_id=core.get("backup_id", "") if isinstance(core.get("backup_id"), str) else "",
        barrier_id=core.get("barrier_id", "") if isinstance(core.get("barrier_id"), str) else "", identity=identity_value,
        bundle_head_oid=authority.get("commit_oid", "") if isinstance(authority, dict) else "",
        object_format=authority.get("object_format", "") if isinstance(authority, dict) else "",
        checks=tuple(checks), blockers=tuple(sorted(blockers)),
        advisories=tuple(sorted(advisories)), verified_at=verified_at,
        closure_r_list_digest=closure.get("r_list_digest") if isinstance(closure, dict)
        and isinstance(closure.get("r_list_digest"), str) else None)

    object.__setattr__(verdict, "_approval_audit", approval_audit)
    return verdict
