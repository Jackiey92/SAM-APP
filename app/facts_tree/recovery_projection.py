"""H: rebuild the read-only projection from one pinned commit in isolation.

When the live repository is gone and only an offline rescue package plus a
pinned historical commit H remain, this module re-creates the complete read
serving set from H's own record bytes: the standard generation, the wide
companion and the FTS companion, then runs the joint publication gate and
reads the bundle back. Everything is built unconditionally; there is no
already-current short circuit.

Scope (F-001 design 98, recovery slice H):

* The isolated bare is opened read-only through the production repository
  adapter (keyword identity) and ``verify_integrity(expected_head=H)`` is
  mandatory, so an ancestor H' or a moved ref is refused before any build.
* Admission uses the host-issued v1 version evidence file: the package
  manifest core digest is recomputed with the public manifest primitives,
  bundle_head must equal the verified H, and every version field is compared
  item by item against this binary's live constants. A missing evidence file
  or field is "unknown"; an explicit mismatch is "incompatible". Both stop
  with zero writes. H never signs compatibility evidence itself.
* The R5 state machine is deliberately untouched (no recovery_epoch or
  recovery_state_store import, no create_case/transition/authenticate/
  merge/open_gate call). H appends one line to its own recovery report and
  returns a structured receipt; phase registration belongs to a later slice.
* A successful projection does not prove the R object closure is complete;
  builders read only H's record bytes, never R originals or mapping objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

from app.facts_tree import projection_advance as adv1
from app.facts_tree import projection_advance_service as service
from app.facts_tree import projection_bundle as bundle_mod
from app.facts_tree import projection_fts as fts
from app.facts_tree import projection_generation as gen
from app.facts_tree import projection_router as router
from app.facts_tree import projection_wide as wide
from app.facts_tree.backup_manifest import compute_core_digest, verify_final_manifest
from app.facts_tree.git_format import encode_id
from app.facts_tree.git_repository import FactsGitRepository

# Host-signed v1 admission evidence envelope (H only reads it).
EVIDENCE_SCHEMA = "sam.f001.recovery_version_evidence"
EVIDENCE_VERSION = 1
VERDICTS = ("compatible", "incompatible", "unknown")
_VERSION_KEYS = (
    "projection_schema_version", "builder_version", "renderer_version",
    "wide_schema_version", "fts_schema_version",
    "tokenizer_version", "tokenizer_dict_sha256",
)
_REPORT_NAME = "recovery-projection-report.jsonl"
# Offline package filenames, pinned in place against the A7 package assembly
# module's MANIFEST_NAME/REPORT_NAME (app/facts_tree, the assembly module,
# lines 164-166). H does not import that module: A7 keeps the package-producing
# import graph unreachable from projection production. The package-check slice
# (recovery_verifier) is the single legacy consumer its boundary allows.
_PACKAGE_MANIFEST_NAME = "manifest.json"
_PACKAGE_REPORT_NAME = "report.json"
_CLOSURE_NOTE = (
    "projection rebuilt from pinned commit; this does not prove the R object "
    "closure is complete"
)


class RecoveryProjectionError(ValueError):
    """The isolated projection rebuild cannot be completed safely."""


class RecoveryProjectionRejected(RecoveryProjectionError):
    """Admission refused before any build; nothing was written."""

    def __init__(self, verdict: str, reason: str):
        if verdict not in ("incompatible", "unknown"):
            raise ValueError("refusal verdict must be incompatible or unknown")
        super().__init__("version evidence " + verdict + ": " + reason)
        self.verdict = verdict
        self.reason = reason


@dataclass(frozen=True)
class ComponentResult:
    path: str
    generation_id: str
    projected_commit: str
    render_sha256: str
    record_count: int
    verified: bool


@dataclass(frozen=True)
class ProjectionRecoveryReceipt:
    case_id: str
    head_oid: str
    genesis_oid: str
    commit_count: int
    object_format: str
    standard: ComponentResult
    wide: ComponentResult
    fts: ComponentResult
    current_pointer_path: str
    published_at: str
    bundle_readback_ok: bool
    report_path: str
    evidence_verdict: str
    evidence_bindings: dict
    projected_commit_matches_head: bool
    closure_completeness_unproven: bool
    r5_state_machine: str


def _str(value, name):
    if type(value) is not str or not value or "\x00" in value:
        raise RecoveryProjectionError(name + " must be a nonempty str without NUL")
    return value


def _path(value, name):
    if not isinstance(value, (str, os.PathLike)) or type(os.fspath(value)) is not str:
        raise RecoveryProjectionError(name + " must be str or os.PathLike[str]")
    path = Path(value)
    if not path.is_absolute():
        raise RecoveryProjectionError(name + " must be an absolute host-pinned path")
    return path


def _json_file(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError("non-finite JSON constant: " + value)

    return json.loads(path.read_text(encoding="utf-8"),
                      object_pairs_hook=unique, parse_constant=bad_constant)


def _stamp(now):
    stamp = now() if now is not None else datetime.now(timezone.utc).isoformat()
    if type(stamp) is not str:
        raise RecoveryProjectionError("now must return a UTC ISO8601 string")
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise RecoveryProjectionError("published time must be UTC ISO8601")
    return stamp


def _live_versions() -> dict:
    # Live version anchors of this binary, compared item by item with the
    # host evidence. wide has no named schema constant: the literal "1"
    # lives at projection_wide.py:_wide_meta (the "wide_schema_version"
    # meta entry); the FTS literal lives at projection_fts.py:_meta
    # ("fts_schema_version"). H matches the literals in place and must not
    # hoist them -- promoting them belongs to the version-contract slice.
    # The tokenizer dictionary digest is projection_fts._DICT_SHA256
    # (projection_fts.py, FTS_TOKENIZER_VERSION's neighbor); it is private
    # today for the same reason. Both build paths fail closed already:
    # record_tree closure validation and the router's versions-must-be-1
    # gate (projection_router.py) reject anything else; this comparison is
    # defense in depth, not the sole barrier.
    return {
        "projection_schema_version": gen.PROJECTION_SCHEMA_VERSION,
        "builder_version": gen.PROJECTION_BUILDER_VERSION,
        "renderer_version": gen.PROJECTION_RENDERER_VERSION,
        "wide_schema_version": "1",
        "fts_schema_version": "1",
        "tokenizer_version": fts.FTS_TOKENIZER_VERSION,
        "tokenizer_dict_sha256": fts._DICT_SHA256,
    }


def _admission(package_dir: Path, evidence_path: Path, head_oid: str):
    """Return (verdict, bindings); never raises for evidence/package failures."""
    live = _live_versions()
    evidence_error = None
    evidence = None
    try:
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise ValueError("version evidence file is absent")
        evidence = _json_file(evidence_path)
        if type(evidence) is not dict:
            raise ValueError("version evidence must be a JSON object")
        if evidence.get("schema") != EVIDENCE_SCHEMA or evidence.get("version") != EVIDENCE_VERSION:
            evidence_error = "evidence schema/version missing or unsupported"
            evidence = None
        elif not all(type(evidence.get(key)) is str and evidence[key]
                     for key in ("core_digest", "bundle_head")):
            evidence_error = "core_digest/bundle_head missing"
            evidence = None
        elif type(evidence.get("verdict")) is not str or evidence["verdict"] not in VERDICTS:
            evidence_error = "evidence verdict missing or unsupported"
            evidence = None
        elif type(evidence.get("versions")) is not dict:
            evidence_error = "versions block missing"
            evidence = None
        else:
            missing = tuple(key for key in _VERSION_KEYS if key not in evidence["versions"])
            if missing:
                evidence_error = "version field missing: " + ",".join(missing)
                evidence = None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "unknown", {"reason": "version evidence cannot be read as v1"}

    if evidence is None:
        return "unknown", {"reason": evidence_error}

    manifest = report = None
    chain_error = None
    try:
        manifest_path = package_dir / _PACKAGE_MANIFEST_NAME
        report_path = package_dir / _PACKAGE_REPORT_NAME
        if not manifest_path.is_file() or not report_path.is_file():
            return "unknown", {"reason": "package manifest/report is absent"}
        manifest = _json_file(manifest_path)
        report = _json_file(report_path)
        verify_final_manifest(manifest, report)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        chain_error = "manifest digest chain verification failed: " + str(exc)
    except Exception as exc:  # untrusted package bytes must never escape admission
        chain_error = "manifest digest chain verification failed: " + str(exc)
    if chain_error is not None:
        return "incompatible", {"reason": chain_error}

    core_digest = compute_core_digest(manifest["core"])
    if evidence["core_digest"] != core_digest:
        return "incompatible", {
            "reason": "evidence core_digest does not bind the package manifest",
            "evidence_core_digest": evidence["core_digest"], "recomputed_core_digest": core_digest}
    if evidence["bundle_head"] != head_oid:
        return "incompatible", {
            "reason": "evidence bundle_head does not equal the pinned verified head",
            "evidence_bundle_head": evidence["bundle_head"], "verified_head": head_oid}

    supplied = evidence["versions"]
    for key in _VERSION_KEYS:
        if type(supplied[key]) is not type(live[key]) or supplied[key] != live[key]:
            return "incompatible", {
                "reason": "version field disagrees with this binary: " + key,
                "field": key, "evidence_value": supplied[key], "live_value": live[key]}
    # H's own recomputation matches every binding. The host's stated
    # conclusion still stands: a host that signs unknown/incompatible may
    # know facts H cannot see (hardware, attestation), so compatibility is
    # granted only when the signed verdict itself says compatible.
    stated = evidence["verdict"]
    if stated != "compatible":
        return stated, {
            "reason": "host-signed verdict is " + stated + " despite matching bindings",
            "stated_verdict": stated}
    return "compatible", {
        "core_digest": core_digest, "bundle_head": head_oid,
        "versions": {key: live[key] for key in _VERSION_KEYS},
        "stated_verdict": "compatible"}


def _component(result, verified=True):
    return ComponentResult(
        str(result.db_path), result.generation_id, result.projected_commit,
        result.render_sha256, result.record_count, bool(verified and result.ready))


def _append_report(report_dir: Path, entry: dict) -> Path:
    router._no_symlinks(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    router._no_symlinks(report_dir)
    path = report_dir / _REPORT_NAME
    created = not path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        seq = 0
        try:
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    seq += 1
                    record = json.loads(line)
                    if type(record) is not dict or record.get("seq") != seq:
                        raise RecoveryProjectionError(
                            "recovery report sequence is not contiguous")
        except (OSError, ValueError) as exc:
            if isinstance(exc, RecoveryProjectionError):
                raise
            raise RecoveryProjectionError("recovery report cannot be replayed: " + str(exc)) from exc
        entry = {**entry, "seq": seq + 1}
        blob = (json.dumps(entry, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        written = os.write(fd, blob)
        if written != len(blob):
            raise RecoveryProjectionError("recovery report append was partial")
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        directory_fd = os.open(str(report_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return path


def rebuild_readonly_projection(*, isolated_repository, head_oid, repo_id,
                                installation_id, company_id, package_dir,
                                projection_root, generations_dir, report_dir,
                                version_evidence, case_id, now=None) -> ProjectionRecoveryReceipt:
    """Rebuild standard+wide+FTS from H in isolation and publish once jointly.

    The signature intentionally has no source-live-repository parameter: the
    live tree cannot exist here (the host additionally hides it with bwrap).
    Refusal raises :class:`RecoveryProjectionRejected` before any write.
    """
    bare = _path(isolated_repository, "isolated_repository")
    package = _path(package_dir, "package_dir")
    root = _path(projection_root, "projection_root")
    directory = _path(generations_dir, "generations_dir")
    reports = _path(report_dir, "report_dir")
    evidence_path = _path(version_evidence, "version_evidence")
    head = _str(head_oid, "head_oid")
    case_id = _str(case_id, "case_id")
    for value, name in ((repo_id, "repo_id"), (installation_id, "installation_id"),
                        (company_id, "company_id")):
        _str(value, name)
    if now is not None and not callable(now):
        raise RecoveryProjectionError("now must be callable")

    # 1. Open the isolated bare with keyword identity and pin H hard. This
    #    refuses ancestor H', moved refs, identity drift and format swaps.
    repository = FactsGitRepository.open(
        bare, repo_id=repo_id, installation_id=installation_id, company_id=company_id)
    integrity = repository.verify_integrity(expected_head=head)
    if (integrity.head_oid != head or integrity.object_format not in ("sha1", "sha256")):
        raise RecoveryProjectionError("integrity report does not pin the requested head")

    # 2. Host v1 admission evidence, recomputed against the package itself.
    verdict, bindings = _admission(package, evidence_path, head)
    if verdict != "compatible":
        raise RecoveryProjectionRejected(verdict, bindings.get("reason", "not compatible"))

    stamp = _stamp(now)

    # Build outputs land only in the host-pinned generations directory; the
    # first admission-passing run is allowed to create it, mirroring the
    # symlink discipline projection_advance_service applies before builds.
    router._no_symlinks(directory)
    directory.mkdir(parents=True, exist_ok=True)
    router._no_symlinks(directory)

    # 3. Unconditional full rebuild, explicit three calls in the exact order
    #    projection_advance_service uses for a bootstrap (service lines
    #    build_standard_generation_for_commit -> build_projection_wide ->
    #    build_projection_fts). No already_current short circuit exists.
    built = adv1.build_standard_generation_for_commit(
        repository, head, str(directory),
        expected_repo_id=repo_id, expected_company_id=company_id)
    wide_built = wide.build_projection_wide(
        built.db_path, directory=str(directory),
        expected_repo_id=repo_id, expected_company_id=company_id)
    fts_built = fts.build_projection_fts(
        built.db_path, directory=str(directory),
        expected_repo_id=repo_id, expected_company_id=company_id)
    for name, derived in (("wide", wide_built), ("fts", fts_built)):
        if (derived.generation_id != built.generation_id
                or derived.projected_commit != built.projected_commit
                or derived.render_sha256 != built.render_sha256 or derived.ready is not True):
            raise RecoveryProjectionError(name + " component identity is not jointly bound")
    if built.projected_commit != head:
        raise RecoveryProjectionError("standard generation is not projected from H")

    # 4. Joint publication through the public gate alias (M2). The pointer
    #    path/format is owned entirely by the router.
    serving = service.publish_projection_bundle(
        root, str(directory), built, fts_built,
        expected_repo_id=repo_id, expected_company_id=company_id, now=lambda: stamp)
    if (serving.projected_commit != head or serving.head_oid != head
            or serving.object_format != integrity.object_format):
        raise RecoveryProjectionError("published serving identity does not equal H")

    # 5. Read the bundle back through the public read-only handle and close
    #    it; degraded reads are never opened during recovery.
    with bundle_mod.open_bundle(root, repo_id=repo_id, company_id=company_id) as opened:
        readback_ok = (
            opened.serving == serving
            and opened.standard.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            and opened.derived.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
    if not readback_ok:
        raise RecoveryProjectionError("post-publication bundle readback failed")

    pointer_path = (root / encode_id(repo_id) / encode_id(company_id) / "current.json")
    try:
        pointer = _json_file(pointer_path)
        published_at = pointer["published_at"]
    except (OSError, ValueError, KeyError) as exc:
        raise RecoveryProjectionError("published pointer cannot be read back: " + str(exc)) from exc
    if published_at != stamp:
        raise RecoveryProjectionError("pointer published_at disagrees with the publication stamp")

    standard = _component(built)
    wide_result = _component(wide_built)
    fts_result = _component(fts_built)

    def file_digest(component):
        body = Path(component.path).read_bytes()
        return {"bytes": len(body), "file_sha256": hashlib.sha256(body).hexdigest()}

    digests = {name: file_digest(component)
               for name, component in (("standard", standard), ("wide", wide_result), ("fts", fts_result))}
    entry = {
        "kind": "recovery_projection",
        "case_id": case_id,
        "head_oid": head,
        "genesis_oid": integrity.genesis_oid,
        "commit_count": integrity.commit_count,
        "object_format": integrity.object_format,
        "products": {
            "standard": {"path": standard.path, "generation_id": standard.generation_id,
                         "render_sha256": standard.render_sha256, "record_count": standard.record_count,
                         **digests["standard"]},
            "wide": {"path": wide_result.path, "generation_id": wide_result.generation_id,
                     "render_sha256": wide_result.render_sha256, "record_count": wide_result.record_count,
                     **digests["wide"]},
            "fts": {"path": fts_result.path, "generation_id": fts_result.generation_id,
                    "render_sha256": fts_result.render_sha256, "record_count": fts_result.record_count,
                    **digests["fts"]}},
        "current_pointer": {"path": str(pointer_path), "published_at": published_at},
        "evidence": {"verdict": verdict, **bindings},
        "closure_note": _CLOSURE_NOTE,
    }
    report_path = _append_report(reports, entry)

    return ProjectionRecoveryReceipt(
        case_id=case_id, head_oid=head, genesis_oid=integrity.genesis_oid,
        commit_count=integrity.commit_count, object_format=integrity.object_format,
        standard=standard, wide=wide_result, fts=fts_result,
        current_pointer_path=str(pointer_path), published_at=published_at,
        bundle_readback_ok=True, report_path=str(report_path),
        evidence_verdict=verdict,
        evidence_bindings={key: value for key, value in bindings.items()
                           if key in ("core_digest", "bundle_head", "stated_verdict")},
        projected_commit_matches_head=True, closure_completeness_unproven=True,
        r5_state_machine="无（state store 未触碰）/ 报告路径=" + str(report_path))
