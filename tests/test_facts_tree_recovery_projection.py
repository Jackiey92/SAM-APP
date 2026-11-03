"""H slice: rebuild the read-only projection triple from one pinned commit.

Real business tree (facts_tree_full_fixture.full_tree through the D15
scoped_tree helper), real Git bare repositories in both sha1 and sha256
object formats, a real git bundle, a real v2 manifest digest chain, real
host-signed version evidence files, and the production publication gate.
Builders and hashes are never mocked.

The independent oracle never calls a builder again: it reads the pinned
snapshot through the production Git adapter, recomputes the render digest
from canonical bytes itself, decodes rows with the public D6
``decode_rows`` primitive, and recomputes every FTS string with
``tokenize_text``.

The isolated bare is a **pre-C synthetic** fixture: C slice (trusted bare
bootstrap) is not on main yet, so the fixture builds the source bare with
production initialize/accept and performs C's documented landing steps by
hand (empty bare init, fsync policy write + read-back, bundle
verify/list-heads/unbundle, exact object-set equality, create-only ref,
symbolic HEAD, production keyword open + integrity pin). It deliberately
does NOT implement C's retained-bare hardening *logic* in production
(hooks/alternates/include sweeping, fsync policy enforcement, branch
setup): every step here is a fixture-side mechanical hand-replay of
c_freeze revB section 2 and is deleted wholesale when C lands.

The rescue package is a minimal A2-shaped three-file package (real
``authority.bundle`` + manifest/report sealed exclusively through the
public manifest primitives). The W/R/R2 payload legs belong to other
slices; H binds only the manifest core digest, the bundle head and the
host version evidence.
"""
from __future__ import annotations

import ast
from collections import Counter
from contextlib import closing
from copy import deepcopy
import hashlib
import inspect
import itertools
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
from types import SimpleNamespace

import pytest

from app.facts_tree import (
    git_format,
    projection_advance as adv1,
    projection_advance_service as service,
    projection_bundle as bundle_mod,
    projection_decode,
    projection_fts as fts,
    projection_generation as gen,
    projection_router as router,
    projection_wide as wide,
    recovery_projection as rp,
)
from app.facts_tree.backup_assembly import MANIFEST_NAME, REPORT_NAME
from app.facts_tree.backup_manifest import (
    build_final_manifest,
    build_verification_report,
    compute_core_digest,
    verify_final_manifest,
)
from app.facts_tree.git_repository import (
    FactsGitRepository,
    GitRepositoryError,
    _FSYNC_POLICY,
    maintenance_pin_ref,
)
from app.facts_tree.record_schema import SCHEMA_COLUMNS, SEARCHABLE_COLUMNS
from app.facts_tree.recovery_state_store import RecoveryStateStore

from tests.test_projection_fault_drills_d15 import scoped_tree
from tests.test_projection_incremental import _append

STAMP = "2026-11-03T00:00:00+00:00"
INST = "install-h-probe"
META_PATH = git_format.record_path("repository_meta")
_GIT = shutil.which("git")
assert _GIT is not None, "H recovery tests require a real git on PATH"


# ---------------------------------------------------------------------------
# Real Git helpers (fixture side; argv lists only, hardened environment)
# ---------------------------------------------------------------------------


def _git_env(base: Path) -> dict:
    for name in ("git-home", "git-tmp", "empty-template"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "HOME": str(base / "git-home"),
        "TMPDIR": str(base / "git-tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TEMPLATE_DIR": str(base / "empty-template"),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args, *, env, stdin=None):
    """Real git subprocess; never a shell string."""
    proc = subprocess.run([_GIT, *args], env=env, input=stdin,
                          capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    return proc


def export_facts_bundle(source_bare: Path, bundle_path: Path, env) -> None:
    """Fixture stand-in for the package H leg: a real git bundle of one ref."""
    _git(["--git-dir", str(source_bare), "bundle", "create", str(bundle_path),
          "refs/heads/facts"], env=env)


def _all_objects(bare: Path, env):
    return {line for line in _git(
        ["--git-dir", str(bare), "cat-file", "--batch-all-objects",
         "--batch-check=%(objectname)"], env=env).stdout.decode().splitlines() if line}


def _rev_list_objects(bare: Path, head: str, env):
    return {line for line in _git(
        ["--git-dir", str(bare), "rev-list", "--objects", "--no-object-names", head],
        env=env).stdout.decode().splitlines() if line}


def _materialize_steps(bundle_path: Path, dest: Path, fmt: str,
                       head: str, env):
    """pre-C synthetic landing: c_freeze revB section 2, replayed by hand.

    The fsync policy write + read-back is one documented C landing step;
    the values come from the production adapter's own ``_FSYNC_POLICY``
    constant (open() refuses a bare that drifts). No H production code
    implements C policy/sweeping: when C lands this whole helper is
    replaced by C's production materializer.
    """
    assert not dest.exists()
    _git(["init", "--bare", "--object-format=" + fmt, "--template=",
          "--initial-branch=facts", str(dest)], env=env)
    assert _git(["--git-dir", str(dest), "symbolic-ref", "HEAD"],
                env=env).stdout.decode().strip() == "refs/heads/facts"
    for key, value in _FSYNC_POLICY:
        _git(["--git-dir", str(dest), "config", "--local", key, value], env=env)
        assert _git(["--git-dir", str(dest), "config", "--local",
                     "--get-all", key], env=env).stdout.decode().splitlines() == [value]
    _git(["--git-dir", str(dest), "bundle", "verify", str(bundle_path)], env=env)
    heads = []
    for line in _git(["--git-dir", str(dest), "bundle", "list-heads",
                      str(bundle_path)], env=env).stdout.decode().splitlines():
        if line.strip():
            heads.append(tuple(line.split()))
    assert heads == [(head, "refs/heads/facts")]
    _git(["--git-dir", str(dest), "bundle", "unbundle", str(bundle_path)], env=env)
    return _all_objects(dest, env), _rev_list_objects(dest, head, env)


def materialize_pre_c_isolated(bundle_path: Path, dest: Path, fmt: str,
                               head: str, env) -> None:
    objects, closure = _materialize_steps(bundle_path, dest, fmt, head, env)
    assert objects == closure, "pre-C recipe: a smuggled object would be refused by C"
    hooks = list((dest / "hooks").iterdir()) if (dest / "hooks").exists() else []
    assert hooks == []
    assert not (dest / "objects" / "info" / "alternates").exists()
    assert not (dest / "objects" / "info" / "grafts").exists()
    config_text = _git(["--git-dir", str(dest), "config", "--local", "--list"],
                       env=env).stdout.decode()
    assert "remote." not in config_text and "include.path" not in config_text
    _git(["--git-dir", str(dest), "update-ref", "--stdin"], env=env,
         stdin=("create refs/heads/facts %s\n" % head).encode())
    refs = _git(["--git-dir", str(dest), "for-each-ref", "--format=%(refname)"],
                env=env).stdout.decode().splitlines()
    assert refs == ["refs/heads/facts"]


# ---------------------------------------------------------------------------
# Scene: real source bare + real package + pre-C isolated bare + case layout
# ---------------------------------------------------------------------------


def _build_package(source: Path, work: Path, name: str, env, scope, fmt,
                   head: str, genesis: str, commit_count: int, tag: str):
    bundle_path = work / (name + ".bundle")
    export_facts_bundle(source, bundle_path, env)
    bundle_bytes = bundle_path.read_bytes()
    backup_id = "bkp-h-" + tag
    core = {
        "schema": "sam.f001.backup_assembly_core",
        "version": 1,
        "digest_algorithm": "sha256",
        "backup_id": backup_id,
        "barrier_id": "barrier-h",
        "identity": {
            "repo_id": scope["repo_id"], "installation_id": INST,
            "company_id": scope["company_id"]},
        "authority": {
            "ref": "refs/heads/facts",
            "commit_oid": head,
            "object_format": fmt,
            "genesis_oid": genesis,
            "commit_count": commit_count,
            "pin_ref": maintenance_pin_ref(backup_id, "barrier-h"),
        },
        "bundle": {
            "file": "authority.bundle",
            "size_bytes": len(bundle_bytes),
            "sha256_digest": "sha256:" + hashlib.sha256(bundle_bytes).hexdigest(),
            "single_pin_ref": True,
            "advertised_refs": ["refs/heads/facts"],
        },
    }
    report = build_verification_report(core, checks={"host": "h-test-fixture"})
    manifest = build_final_manifest(core, report)
    verify_final_manifest(manifest, report)
    package_dir = work / name
    package_dir.mkdir()
    (package_dir / "authority.bundle").write_bytes(bundle_bytes)
    (package_dir / MANIFEST_NAME).write_bytes(git_format.dumps_record(manifest))
    (package_dir / REPORT_NAME).write_bytes(git_format.dumps_record(report))
    return package_dir, core, compute_core_digest(core)


def _safe_tag(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in text)[:48]


def make_case(base: Path, tag: str, fmt: str = "sha1"):
    tag = _safe_tag(tag)
    scope = {"repo_id": "repo-h-" + tag, "company_id": "co-h-" + tag}
    work = base / ("work-" + tag)
    source = work / "source.git"
    repo = FactsGitRepository.initialize(source, installation_id=INST,
                                         object_format=fmt, **scope)
    genesis = repo.head_oid()
    tree = scoped_tree(scope, genesis)
    tree[META_PATH] = git_format.loads_record(
        repo.read_snapshot_at(genesis).records[0].content)
    c1 = _append(repo, tree)
    changed = deepcopy(tree)
    changed[git_format.record_path("fact_current", fact_uid="fact-1")]["status"] = "retracted"
    c2 = _append(repo, changed)
    integrity = repo.verify_integrity(expected_head=c2)

    env = _git_env(work)
    package, core, core_digest = _build_package(
        source, work, "package", env, scope, fmt, c2, genesis,
        integrity.commit_count, tag)
    isolated = work / "isolated.git"
    materialize_pre_c_isolated(package / "authority.bundle", isolated,
                               fmt, c2, env)
    opened = FactsGitRepository.open(isolated, repo_id=scope["repo_id"],
                                     installation_id=INST, company_id=scope["company_id"])
    assert opened.verify_integrity(expected_head=c2).head_oid == c2

    case = work / "case"
    case.mkdir(mode=0o700)
    # Real R5 state-store anchor, so zero-write snapshots cover the exact
    # file recovery_state_store.py owns. H itself never imports that store.
    state = RecoveryStateStore(case)
    state.close()
    evidence_path = work / "host-version-evidence.json"
    ctx = SimpleNamespace(
        fmt=fmt, scope=scope, source=source, isolated=isolated, package=package,
        case=case, root=case / "projection", gens=case / "generations",
        reports=case / "reports", evidence=evidence_path, env=env,
        genesis=genesis, c1=c1, head=c2, work=work, core=core,
        core_digest=core_digest, anchor=case / "recovery-state.sqlite3")
    write_evidence(ctx)
    return ctx


def live_versions():
    # Item-by-item live anchors; the two "1" literals are matched in place at
    # projection_wide.py:_wide_meta and projection_fts.py:_meta, per freeze:
    # H must not hoist them into named constants.
    return {
        "projection_schema_version": gen.PROJECTION_SCHEMA_VERSION,
        "builder_version": gen.PROJECTION_BUILDER_VERSION,
        "renderer_version": gen.PROJECTION_RENDERER_VERSION,
        "wide_schema_version": "1",
        "fts_schema_version": "1",
        "tokenizer_version": fts.FTS_TOKENIZER_VERSION,
        "tokenizer_dict_sha256": fts._DICT_SHA256,
    }


def write_evidence(ctx, *, path=None, verdict="compatible", bundle_head=None,
                   core_digest=None, versions=None, raw=None, drop=None):
    path = path or ctx.evidence
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    doc = {
        "schema": rp.EVIDENCE_SCHEMA,
        "version": rp.EVIDENCE_VERSION,
        "verdict": verdict,
        "core_digest": core_digest if core_digest is not None else ctx.core_digest,
        "bundle_head": bundle_head if bundle_head is not None else ctx.head,
        "versions": versions if versions is not None else live_versions(),
    }
    if drop:
        container, _, key = drop.partition(".")
        if key:
            del doc[container][key]
        else:
            del doc[container]
    path.write_bytes(git_format.dumps_record(doc))
    return path


def rebuild_kwargs(ctx, **overrides):
    kwargs = dict(
        isolated_repository=ctx.isolated, head_oid=ctx.head,
        repo_id=ctx.scope["repo_id"], installation_id=INST,
        company_id=ctx.scope["company_id"], package_dir=ctx.package,
        projection_root=ctx.root, generations_dir=ctx.gens,
        report_dir=ctx.reports, version_evidence=ctx.evidence,
        case_id="case-h", now=lambda: STAMP)
    kwargs.update(overrides)
    return kwargs


def rebuild(ctx, **overrides):
    return rp.rebuild_readonly_projection(**rebuild_kwargs(ctx, **overrides))


def repin(ctx, head, tag):
    """Materialize a second package+isolated pair pinned at an older commit.

    Moves the fixture-owned source ref temporarily (the object database is
    untouched), exports a real bundle advertising refs/heads/facts at the
    pin, and lands a second pre-C isolated bare alongside the first.
    """
    _git(["--git-dir", str(ctx.source), "update-ref",
          "refs/heads/facts", head], env=ctx.env)
    try:
        probe = FactsGitRepository.open(
            ctx.source, repo_id=ctx.scope["repo_id"],
            installation_id=INST, company_id=ctx.scope["company_id"])
        commit_count = probe.verify_integrity(expected_head=head).commit_count
        package, _, digest = _build_package(
            ctx.source, ctx.work, "package-" + tag, ctx.env, ctx.scope,
            ctx.fmt, head, ctx.genesis, commit_count,
            _safe_tag(tag + "-" + head[:8]))
        isolated = ctx.work / ("isolated-" + tag + ".git")
        materialize_pre_c_isolated(package / "authority.bundle", isolated,
                                   ctx.fmt, head, ctx.env)
        facts = FactsGitRepository.open(
            isolated, repo_id=ctx.scope["repo_id"],
            installation_id=INST, company_id=ctx.scope["company_id"])
        assert facts.verify_integrity(expected_head=head).head_oid == head
    finally:
        _git(["--git-dir", str(ctx.source), "update-ref",
              "refs/heads/facts", ctx.head], env=ctx.env)
    evidence = ctx.work / ("host-version-evidence-" + tag + ".json")
    write_evidence(ctx, path=evidence, bundle_head=head, core_digest=digest)
    return package, isolated, evidence


# ---------------------------------------------------------------------------
# Zero-write fingerprints
# ---------------------------------------------------------------------------


def fingerprint(root: Path):
    if not root.exists():
        return None
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        st = path.lstat()
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif stat.S_ISDIR(st.st_mode):
            out[rel] = ("dir", stat.S_IMODE(st.st_mode))
        elif stat.S_ISREG(st.st_mode):
            out[rel] = ("file", stat.S_IMODE(st.st_mode), path.read_bytes())
        else:
            raise AssertionError("unexpected case entry: " + rel)
    return out


def assert_case_zero_writes(ctx, before):
    assert fingerprint(ctx.case) == before
    assert not ctx.root.exists() and not ctx.gens.exists() and not ctx.reports.exists()


# ---------------------------------------------------------------------------
# Independent oracle (snapshot + decode_rows + tokenize_text; never a builder)
# ---------------------------------------------------------------------------


def _pointer_of(root, scope):
    return (root / git_format.encode_id(scope["repo_id"])
            / git_format.encode_id(scope["company_id"]) / "current.json")


def independent_oracle(ctx, receipt=None):
    """Re-derive every published byte from H through public read primitives."""
    repo = FactsGitRepository.open(ctx.isolated, repo_id=ctx.scope["repo_id"],
                                   installation_id=INST, company_id=ctx.scope["company_id"])
    snapshot = repo.read_snapshot_at(ctx.head)
    expected = {}
    for record in snapshot.records:
        family = git_format.canonical_record_type(record.path)
        expected[record.path] = (family, record.content)
    expected_render = b"\x1e".join(
        family.encode() + b"\x1f" + path.encode() + b"\x1f" + content
        for path, (family, content) in sorted(expected.items()))
    expected_render_sha = hashlib.sha256(expected_render).hexdigest()

    pointer_path = _pointer_of(ctx.root, ctx.scope)
    pointer = json.loads(pointer_path.read_text())
    assert set(pointer) == router._POINTER_KEYS
    assert pointer["format"] == "projection-current@1"
    standard_path = Path(pointer["db_path"])
    derived_path = standard_path.parent / (
        "derived-" + pointer["generation_id"] + ".sqlite")
    with closing(sqlite3.connect(standard_path.as_uri() + "?mode=ro", uri=True)) as std:
        meta = dict(std.execute("SELECT key,value FROM generation_meta"))
        rows = std.execute(
            "SELECT path,family,canonical_bytes,byte_length,sha256 "
            "FROM record_canonical ORDER BY path").fetchall()
        counts = dict(std.execute("SELECT family,record_count FROM family_counts"))
        assert meta["status"] == "ready"
        assert meta["projected_commit"] == ctx.head == meta["head_oid"]
        assert meta["object_format"] == ctx.fmt
        assert int(meta["record_count"]) == len(rows) == len(expected)
        for path, family, content, length, digest in rows:
            exp_family, exp_content = expected[path]
            assert family == exp_family and content == exp_content
            assert len(content) == length
            assert hashlib.sha256(content).hexdigest() == digest
        assert meta["render_sha256"] == expected_render_sha
        assert counts == dict(Counter(family for family, _ in expected.values()))
        decoded = projection_decode.decode_rows(
            std, [(p, f, n, h) for p, f, _, n, h in rows],
            authz_digest="D8-local-build")
    decoded_by_path = {r.path: (r.family, dict(r.payload)) for r in decoded.records}

    # FTS oracle: tokenize every searchable column string independently,
    # following the established test_projection_fts.py expected-row pattern.
    fts_expected = []
    for record in sorted(decoded.records, key=lambda r: r.path):
        raw_text = " ".join(
            record.payload[name] for name in SEARCHABLE_COLUMNS[record.family]
            if type(record.payload.get(name)) is str and record.payload[name].strip())
        fts_expected.append((record.path, record.family, fts.tokenize_text(raw_text)))

    with closing(sqlite3.connect(derived_path.as_uri() + "?mode=ro", uri=True)) as der:
        assert der.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        wide_meta = dict(der.execute("SELECT key,value FROM wide_meta"))
        fts_meta = dict(der.execute("SELECT key,value FROM fts_meta"))
        assert wide_meta["wide_schema_version"] == "1" == fts_meta["fts_schema_version"]
        assert fts_meta["tokenizer"] == fts.FTS_TOKENIZER_VERSION
        assert fts_meta["jieba_dict_sha256"] == fts._DICT_SHA256
        assert der.execute(
            "SELECT path,family,record_text FROM fts_records ORDER BY path").fetchall() == fts_expected

        pointers, placements = {}, {}
        for path, (family, payload) in decoded_by_path.items():
            if family == "placement_current":
                pointers[payload["fact_uid"]] = payload["revision"]
            elif family == "placement_revision":
                placements[(payload["fact_uid"], payload["revision"])] = payload
        canonical = {p: c for p, (_, c) in expected.items()}
        for family in SCHEMA_COLUMNS:
            columns = tuple(SCHEMA_COLUMNS[family])
            info = der.execute("PRAGMA table_info(dw_" + family + ")").fetchall()
            stored = der.execute(
                "SELECT * FROM dw_" + family + " ORDER BY path").fetchall()
            matching = {p: pair for p, pair in decoded_by_path.items()
                        if pair[0] == family}
            ownership = len(wide._OWNERSHIP) if family == "fact_current" else 0
            offset = len(columns) + ownership
            assert [col[1] for col in info[:len(columns)]] == [c.name for c in columns]
            if family == "fact_current":
                assert [col[1] for col in info[len(columns):offset]] == list(wide._OWNERSHIP)
            assert [col[1] for col in info[offset:]] == [
                "path", "payload_json", "canonical_sha256", "byte_length"]
            assert len(stored) == len(matching)
            for row in stored:
                assert len(row) == offset + 4
                payload = matching[row[offset]][1]
                for scalar, column in zip(row[:len(columns)], columns):
                    assert scalar == payload.get(column.name)
                    if column.sql_type == "TEXT":
                        assert scalar is None or type(scalar) is str
                    elif column.sql_type == "INTEGER":
                        assert scalar is None or type(scalar) is int
                    elif column.sql_type == "REAL":
                        assert scalar is None or type(scalar) is float
                if family == "fact_current":
                    placement = placements[(payload["fact_uid"], pointers[payload["fact_uid"]])]
                    assert tuple(row[len(columns):offset]) == tuple(
                        placement[name] for name in wide._OWNERSHIP)
                    assert placement["fact_ref"] == {
                        "record_type": "fact_revision", "stable_id": payload["fact_uid"],
                        "revision": payload["revision"]}
                record_path, payload_json, digest, length = row[offset:]
                assert record_path in matching
                reencoded = git_format.dumps_record(git_format.loads_record(payload_json))
                assert reencoded == canonical[record_path]
                assert length == len(reencoded)
                assert hashlib.sha256(reencoded).hexdigest() == digest

    with bundle_mod.open_bundle(ctx.root, repo_id=ctx.scope["repo_id"],
                                company_id=ctx.scope["company_id"]) as opened:
        assert opened.serving.projected_commit == ctx.head
        assert opened.serving.render_sha256 == expected_render_sha

    if receipt is not None:
        assert receipt.projected_commit_matches_head is True
        assert receipt.closure_completeness_unproven is True
        assert receipt.head_oid == ctx.head and receipt.object_format == ctx.fmt
        assert receipt.standard.path == str(standard_path)
        assert receipt.wide.path == receipt.fts.path == str(derived_path)
        assert {c.generation_id for c in (receipt.standard, receipt.wide, receipt.fts)} == {
            pointer["generation_id"]}
        assert all(c.verified and c.projected_commit == ctx.head
                   for c in (receipt.standard, receipt.wide, receipt.fts))
        assert receipt.evidence_verdict == "compatible"
        assert receipt.evidence_bindings["core_digest"] == ctx.core_digest
        assert receipt.evidence_bindings["bundle_head"] == ctx.head
        assert receipt.r5_state_machine == (
            "无（state store 未触碰）/ 报告路径=" + receipt.report_path)
    return pointer, expected_render_sha


def bundle_content(root, scope):
    """Dir-independent content fingerprint; shared with the disaster drills."""
    pointer_path = _pointer_of(root, scope)
    pointer = json.loads(pointer_path.read_text())
    standard_path = Path(pointer["db_path"])
    derived_path = standard_path.parent / (
        "derived-" + pointer["generation_id"] + ".sqlite")
    identity = {key: value for key, value in pointer.items() if key != "db_path"}
    identity["db_basename"] = standard_path.name
    with closing(sqlite3.connect(standard_path.as_uri() + "?mode=ro", uri=True)) as std:
        standard = {
            "meta": dict(std.execute("SELECT key,value FROM generation_meta")),
            "rows": std.execute(
                "SELECT path,family,canonical_bytes,byte_length,sha256 "
                "FROM record_canonical ORDER BY path").fetchall(),
            "counts": std.execute(
                "SELECT family,record_count FROM family_counts ORDER BY family").fetchall(),
            "fact_view": std.execute(
                "SELECT * FROM fact_current_view ORDER BY 1").fetchall(),
            "placement_view": std.execute(
                "SELECT * FROM placement_current_view ORDER BY 1").fetchall(),
        }
    with closing(sqlite3.connect(derived_path.as_uri() + "?mode=ro", uri=True)) as der:
        derived = {
            "wide_meta": dict(der.execute("SELECT key,value FROM wide_meta")),
            "fts_meta": dict(der.execute("SELECT key,value FROM fts_meta")),
            "fts_rows": der.execute(
                "SELECT path,family,record_text FROM fts_records ORDER BY path").fetchall(),
            "dw": {family: der.execute(
                "SELECT * FROM dw_" + family + " ORDER BY path").fetchall()
                for family in SCHEMA_COLUMNS},
        }
    return identity, standard, derived


def physical_inventory(directory: Path):
    return {p.name: {"bytes": p.stat().st_size,
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(directory.iterdir()) if p.is_file()}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=["sha1", "sha256"])
def ctx_factory(tmp_path, request):
    counter = itertools.count(1)

    def factory():
        return make_case(
            tmp_path,
            "%d-%s-%s" % (next(counter), request.param,
                          request.node.name.replace("/", "_")),
            request.param)
    return factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_entry_signature_has_no_live_repo_and_module_is_pure():
    sig = inspect.signature(rp.rebuild_readonly_projection)
    assert all(param.kind == inspect.Parameter.KEYWORD_ONLY
               for param in sig.parameters.values())
    forbidden = ("source", "live", "upstream", "original_repo")
    assert not any(any(token == name or token in name.split("_")
                       for token in forbidden) for name in sig.parameters)
    source = Path(rp.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_imports = {"recovery_epoch", "recovery_state_store", "subprocess",
                      "socket", "pickle", "http", "urllib", "requests",
                      "smtplib", "ftplib", "marshal", "telnetlib", "xmlrpc"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned_imports), imported & banned_imports
    for token in ("Popen", "os.system", "socket", "pickle", "__import__", "eval("):
        assert token not in source, token


def test_public_alias_is_exactly_the_private_gate():
    # M2: the one allowed production-file change, pinned against drift.
    assert service.publish_projection_bundle is service._publish_bundle
    source = inspect.getsource(service)
    assert source.count("publish_projection_bundle = _publish_bundle") == 1
    assert service.publish_projection_bundle.__name__ == "_publish_bundle"


def test_happy_rebuild_triple_pointer_report_and_independent_oracle(ctx_factory):
    ctx = ctx_factory()
    anchor_before = ctx.anchor.read_bytes()
    receipt = rebuild(ctx)
    assert receipt.bundle_readback_ok is True
    pointer, render_sha = independent_oracle(ctx, receipt)
    # The R5 anchor exists but H never writes a byte into it.
    assert ctx.anchor.read_bytes() == anchor_before
    products = sorted(p.name for p in ctx.gens.iterdir())
    assert products == ["derived-" + pointer["generation_id"] + ".sqlite",
                        "gen-" + pointer["generation_id"] + ".sqlite"]
    assert len(pointer["generation_id"]) == 32
    assert pointer["projected_commit"] == ctx.head
    assert pointer["published_at"] == STAMP
    lines = [json.loads(line) for line in
             Path(receipt.report_path).read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["seq"] == 1
    entry = lines[0]
    assert entry["object_format"] == ctx.fmt and entry["head_oid"] == ctx.head
    assert "does not prove the R object closure is complete" in entry["closure_note"]
    for name in ("standard", "wide", "fts"):
        product = entry["products"][name]
        assert product["render_sha256"] == render_sha
        assert len(product["file_sha256"]) == 64 and product["bytes"] > 0
        assert Path(product["path"]).is_file()
    assert entry["products"]["wide"]["path"] == entry["products"]["fts"]["path"]
    assert entry["evidence"]["verdict"] == "compatible"
    assert entry["current_pointer"]["path"] == receipt.current_pointer_path
    with sqlite3.connect(ctx.anchor) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"recovery_events", "recovery_watermark"} <= tables


def test_rerun_converges_without_new_generation_or_pointer_change(ctx_factory):
    ctx = ctx_factory()
    first = rebuild(ctx)
    pointer_bytes = Path(first.current_pointer_path).read_bytes()
    inventory = physical_inventory(ctx.gens)
    second = rebuild(ctx)
    assert second.standard.generation_id == first.standard.generation_id
    assert Path(second.current_pointer_path).read_bytes() == pointer_bytes
    after = physical_inventory(ctx.gens)
    assert set(after) == set(inventory) == {
        "gen-" + first.standard.generation_id + ".sqlite",
        "derived-" + first.standard.generation_id + ".sqlite"}
    assert all(after[name] == inventory[name] for name in inventory)
    leftovers = [p.name for p in ctx.gens.iterdir() if p.name.startswith(".")]
    assert leftovers == []
    lines = [json.loads(line) for line in
             Path(second.report_path).read_text().splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    independent_oracle(ctx, second)


def test_missing_evidence_is_unknown_with_zero_writes(ctx_factory):
    ctx = ctx_factory()
    ctx.evidence.unlink()
    before = fingerprint(ctx.case)
    with pytest.raises(rp.RecoveryProjectionRejected) as raised:
        rebuild(ctx)
    assert raised.value.verdict == "unknown"
    assert_case_zero_writes(ctx, before)


def test_missing_package_manifest_is_unknown_with_zero_writes(ctx_factory):
    ctx = ctx_factory()
    before = fingerprint(ctx.case)
    (ctx.package / MANIFEST_NAME).unlink()
    with pytest.raises(rp.RecoveryProjectionRejected) as raised:
        rebuild(ctx)
    assert raised.value.verdict == "unknown"
    assert_case_zero_writes(ctx, before)


def test_malformed_or_wrong_envelope_evidence_is_unknown(ctx_factory):
    for raw in ("{not json", json.dumps({"schema": "other", "version": 1}),
                json.dumps({"schema": rp.EVIDENCE_SCHEMA, "version": 9})):
        ctx = ctx_factory()
        before = fingerprint(ctx.case)
        write_evidence(ctx, raw=raw)
        with pytest.raises(rp.RecoveryProjectionRejected) as raised:
            rebuild(ctx)
        assert raised.value.verdict == "unknown"
        assert_case_zero_writes(ctx, before)


def test_missing_version_field_is_unknown(ctx_factory):
    for drop in ("versions.tokenizer_version",
                 "versions.projection_schema_version",
                 "verdict", "core_digest", "bundle_head"):
        ctx = ctx_factory()
        before = fingerprint(ctx.case)
        write_evidence(ctx, drop=drop)
        with pytest.raises(rp.RecoveryProjectionRejected) as raised:
            rebuild(ctx)
        assert raised.value.verdict == "unknown", drop
        assert_case_zero_writes(ctx, before)


def test_host_signed_non_compatible_verdict_refused_even_when_bindings_match(ctx_factory):
    for verdict in ("unknown", "incompatible"):
        ctx = ctx_factory()
        before = fingerprint(ctx.case)
        write_evidence(ctx, verdict=verdict)
        with pytest.raises(rp.RecoveryProjectionRejected) as raised:
            rebuild(ctx)
        assert raised.value.verdict == verdict
        assert_case_zero_writes(ctx, before)


def test_every_live_version_mismatch_is_incompatible_with_zero_writes(tmp_path):
    base = live_versions()
    overrides = {
        "projection_schema_version": 999,
        "builder_version": 999,
        "renderer_version": 999,
        "wide_schema_version": "2",
        "fts_schema_version": "2",
        "tokenizer_version": "other-tokenizer-v0",
        "tokenizer_dict_sha256": "0" * 64,
    }
    for index, (key, bad) in enumerate(overrides.items()):
        ctx = make_case(tmp_path, "mismatch-%d-%s" % (index, key), "sha1")
        before = fingerprint(ctx.case)
        versions = dict(base)
        versions[key] = bad
        write_evidence(ctx, versions=versions)
        with pytest.raises(rp.RecoveryProjectionRejected) as raised:
            rebuild(ctx)
        assert raised.value.verdict == "incompatible", key
        assert key in raised.value.reason
        assert_case_zero_writes(ctx, before)


def test_unbound_core_digest_bundle_head_and_chain_tamper_are_incompatible(tmp_path):
    ctx = make_case(tmp_path, "chain-tamper", "sha1")
    before = fingerprint(ctx.case)
    write_evidence(ctx, core_digest=compute_core_digest({"different": "core"}))
    with pytest.raises(rp.RecoveryProjectionRejected) as raised:
        rebuild(ctx)
    assert raised.value.verdict == "incompatible"
    assert "core_digest" in raised.value.reason
    assert_case_zero_writes(ctx, before)

    write_evidence(ctx, bundle_head=ctx.c1)
    with pytest.raises(rp.RecoveryProjectionRejected) as raised:
        rebuild(ctx)
    assert raised.value.verdict == "incompatible"
    assert "bundle_head" in raised.value.reason
    assert_case_zero_writes(ctx, before)

    report_path = ctx.package / REPORT_NAME
    tampered_report = git_format.loads_record(report_path.read_bytes())
    tampered_report["checks"]["host"] = "tampered"
    report_path.write_bytes(git_format.dumps_record(tampered_report))
    write_evidence(ctx)
    with pytest.raises(rp.RecoveryProjectionRejected) as raised:
        rebuild(ctx)
    assert raised.value.verdict == "incompatible"
    assert_case_zero_writes(ctx, before)


def test_ancestor_head_moved_ref_and_identity_are_refused(tmp_path):
    ctx = make_case(tmp_path, "head-negatives", "sha1")
    before = fingerprint(ctx.case)

    with pytest.raises(GitRepositoryError):
        rebuild(ctx, head_oid=ctx.c1)
    assert_case_zero_writes(ctx, before)

    _git(["--git-dir", str(ctx.isolated), "update-ref",
          "refs/heads/facts", ctx.c1], env=ctx.env)
    try:
        with pytest.raises(GitRepositoryError):
            rebuild(ctx)
    finally:
        _git(["--git-dir", str(ctx.isolated), "update-ref",
              "refs/heads/facts", ctx.head], env=ctx.env)
    assert_case_zero_writes(ctx, before)

    with pytest.raises(GitRepositoryError):
        rebuild(ctx, company_id="co-h-wrong-identity")
    assert_case_zero_writes(ctx, before)


def test_sha1_sha256_swap_and_foreign_head_are_refused(tmp_path):
    sha256_ctx = make_case(tmp_path, "swap-sha256-a", "sha256")
    sha1_ctx = make_case(tmp_path, "swap-sha1", "sha1")
    other = make_case(tmp_path, "swap-sha256-b", "sha256")
    before = fingerprint(sha256_ctx.case)

    def attempt(base_ctx, head, case_id):
        # sha1↔sha256 length swap dies in the adapter's oid precondition
        # (plain ValueError); a foreign same-length oid dies in
        # verify_integrity (GitRepositoryError). Both refuse pre-write.
        with pytest.raises((GitRepositoryError, ValueError, rp.RecoveryProjectionError)):
            rp.rebuild_readonly_projection(
                isolated_repository=base_ctx.isolated, head_oid=head,
                repo_id=base_ctx.scope["repo_id"], installation_id=INST,
                company_id=base_ctx.scope["company_id"], package_dir=base_ctx.package,
                projection_root=base_ctx.root, generations_dir=base_ctx.gens,
                report_dir=base_ctx.reports, version_evidence=base_ctx.evidence,
                case_id=case_id, now=lambda: STAMP)

    attempt(sha256_ctx, sha1_ctx.head, "case-swap-length")
    assert_case_zero_writes(sha256_ctx, before)
    attempt(sha256_ctx, other.head, "case-swap-foreign")
    assert fingerprint(sha256_ctx.case) == before
    # A rejection must never be a published RecoveryProjectionRejected claim
    # with side effects: no report, no generations anywhere in the case root.
    assert not (sha256_ctx.case / "reports").exists()


def test_same_generation_tamper_fails_closed_then_rebuilds_after_quarantine(tmp_path):
    ctx = make_case(tmp_path, "tamper", "sha1")
    receipt = rebuild(ctx)
    derived = Path(receipt.fts.path)
    baseline_content = bundle_content(ctx.root, ctx.scope)
    pointer_bytes = Path(receipt.current_pointer_path).read_bytes()
    inventory = physical_inventory(ctx.gens)
    with sqlite3.connect(derived) as connection:
        with connection:
            connection.execute("UPDATE fts_records SET record_text='zz-tamper'")
    tampered_inventory = physical_inventory(ctx.gens)

    # Same-generation "staleness" can physically only be content tamper:
    # the builders preserve an invalid same-name file and fail closed; no
    # local patch logic exists, the pointer is never moved, the report is
    # not appended and no temp artifact survives.
    with pytest.raises(fts.FTSBuildError):
        rebuild(ctx)
    assert Path(receipt.current_pointer_path).read_bytes() == pointer_bytes
    assert physical_inventory(ctx.gens) == tampered_inventory
    lines = Path(receipt.report_path).read_text().splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1]
    with bundle_mod.open_bundle(ctx.root, repo_id=ctx.scope["repo_id"],
                                company_id=ctx.scope["company_id"]) as opened:
        assert opened.serving.generation_id == receipt.standard.generation_id

    # Reentry after the tampered artifact is quarantined by host/operator
    # (the D15 E1 precedent: removal is explicit ops recovery, never a
    # production repair API): an unconditional rebuild recreates the same
    # deterministic identity and content; the pointer identity never moves.
    derived.unlink()
    recovered = rebuild(ctx)
    assert recovered.standard.generation_id == receipt.standard.generation_id
    assert Path(recovered.current_pointer_path).read_bytes() == pointer_bytes
    assert bundle_content(ctx.root, ctx.scope) == baseline_content
    after = physical_inventory(ctx.gens)
    assert set(after) == set(inventory)
    assert after["gen-" + receipt.standard.generation_id + ".sqlite"] == inventory[
        "gen-" + receipt.standard.generation_id + ".sqlite"]
    lines = Path(receipt.report_path).read_text().splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]
    independent_oracle(ctx, recovered)


def test_cross_generation_stale_pointer_migrates_to_h(tmp_path):
    ctx = make_case(tmp_path, "pointer-migration", "sha1")
    package_c1, isolated_c1, evidence_c1 = repin(ctx, ctx.c1, "c1")
    old = rp.rebuild_readonly_projection(
        isolated_repository=isolated_c1, head_oid=ctx.c1,
        repo_id=ctx.scope["repo_id"], installation_id=INST,
        company_id=ctx.scope["company_id"], package_dir=package_c1,
        projection_root=ctx.root, generations_dir=ctx.gens,
        report_dir=ctx.reports, version_evidence=evidence_c1,
        case_id="case-h-c1", now=lambda: STAMP)
    old_files = {p.name for p in ctx.gens.iterdir()}
    assert old.standard.projected_commit == ctx.c1

    new = rebuild(ctx, case_id="case-h-c2")
    assert new.standard.generation_id != old.standard.generation_id
    assert new.standard.projected_commit == ctx.head
    files = {p.name for p in ctx.gens.iterdir()}
    assert old_files <= files and len(files) == len(old_files) + 2
    pointer = json.loads(Path(new.current_pointer_path).read_text())
    assert pointer["projected_commit"] == ctx.head
    lines = [json.loads(line) for line in
             Path(new.report_path).read_text().splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    independent_oracle(ctx, new)


@pytest.mark.parametrize("corruption", ["missing-table", "stale-meta"])
def test_direct_alias_gate_rejects_derived_anomalies(tmp_path, corruption):
    ctx = make_case(tmp_path, "gate-" + corruption, "sha1")
    ids = {"expected_repo_id": ctx.scope["repo_id"],
           "expected_company_id": ctx.scope["company_id"]}
    gate_root = ctx.case / "gate-root"
    gate_gens = ctx.case / ("gate-gens-" + corruption)
    gate_gens.mkdir(parents=True)
    repo = FactsGitRepository.open(ctx.isolated, repo_id=ctx.scope["repo_id"],
                                   installation_id=INST, company_id=ctx.scope["company_id"])
    built = adv1.build_standard_generation_for_commit(
        repo, ctx.head, str(gate_gens), **ids)
    wide.build_projection_wide(built.db_path, directory=str(gate_gens), **ids)
    derived = fts.build_projection_fts(built.db_path, directory=str(gate_gens), **ids)
    with sqlite3.connect(derived.db_path) as connection:
        with connection:
            if corruption == "missing-table":
                connection.execute("DROP TABLE fts_records")
            else:
                connection.execute(
                    "UPDATE fts_meta SET value='stale-generation' WHERE key='generation_id'")
    with pytest.raises(service.AdvanceServiceError):
        service.publish_projection_bundle(
            gate_root, str(gate_gens), built, derived, **ids)
    assert not _pointer_of(gate_root, ctx.scope).exists()


def test_pre_c_recipe_guard_actually_detects_a_smuggled_object(tmp_path):
    ctx = make_case(tmp_path, "smuggled", "sha1")
    dest = ctx.work / "smuggled.git"
    objects, closure = _materialize_steps(
        ctx.package / "authority.bundle", dest, "sha1", ctx.head, ctx.env)
    assert objects == closure
    smuggled = _git(["--git-dir", str(dest), "hash-object", "-w", "--stdin"],
                    env=ctx.env, stdin=b"smuggled payload").stdout.decode().strip()
    all_objects = _all_objects(dest, ctx.env)
    assert smuggled not in closure
    assert all_objects - closure == {smuggled}
    # No create-only ref landed: the production adapter must also refuse
    # the half-materialized bare (no authoritative branch tip).
    with pytest.raises(GitRepositoryError):
        FactsGitRepository.open(dest, repo_id=ctx.scope["repo_id"],
                                installation_id=INST, company_id=ctx.scope["company_id"])
