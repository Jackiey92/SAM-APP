"""Minimal pure in-memory digest chain for F-001 backup manifests.

Implements section 5 of the approved F-001 backup design (manifest and
verification report), reduced to the smallest digest-chain kernel:

1. The core digest is computed from the complete core payload alone,
   via the canonical record serialization (``git_format.dumps_record``)
   and SHA-256.
2. A verification report must reference that core digest; the report's
   own digest is computed from its canonical serialization.
3. The final manifest references the core digest and the verification
   report digest. The report never carries its own digest and no
   final-manifest digest is computed, so the chain contains no
   self-reference or cycle.

Scope guards: this module only accepts in-memory dicts. It never reads
Git, SQLite, disk, objects or real data, and it implements no
collection, backup, persistence, barrier or restore behaviour.
"""
import hashlib
import re

from app.facts_tree.git_format import dumps_record

DIGEST_ALGORITHM = "sha256"
SCHEMA_VERSION = 1
REPORT_SCHEMA = "sam.f001.backup_verification_report"
MANIFEST_SCHEMA = "sam.f001.backup_manifest"

# Field names that would create a self-referencing digest; they are
# forbidden at every nesting level of the chain, inside dicts and
# lists alike.
_SELF_DIGEST_FIELDS = ("report_digest", "manifest_digest")

# The single place where a digest reference is legitimate: the final
# manifest's verification block references the report digest by design.
_ALLOWED_MANIFEST_DIGEST_PATHS = (("verification", "report_digest"),)

_DIGEST_PATTERN = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class BackupManifestError(ValueError):
    """Raised when a digest-chain input is malformed or inconsistent."""


def _require_mapping(value, label):
    if not isinstance(value, dict):
        raise BackupManifestError(f"{label} must be an in-memory dict")


def _require_digest(value, label):
    if not isinstance(value, str) or not _DIGEST_PATTERN.match(value):
        raise BackupManifestError(
            f"{label} must be a '{DIGEST_ALGORITHM}:' + 64 lowercase hex digest"
        )


def _reject_nested_fields(value, fields, label, allowed_paths=(), path=()):
    """Recursively refuse forbidden keys inside nested dicts and lists.

    Traverses every dict and list level of ``value``; a key listed in
    ``fields`` raises :class:`BackupManifestError` unless its exact
    location appears in ``allowed_paths``.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = path + (key,)
            if key in fields and key_path not in allowed_paths:
                where = " at " + "/".join(str(part) for part in key_path) if key_path else ""
                raise BackupManifestError(
                    f"{label} must not carry a self-referencing '{key}' field{where}"
                )
            _reject_nested_fields(item, fields, label, allowed_paths, key_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nested_fields(item, fields, label, allowed_paths, path + (index,))


def compute_core_digest(core_payload):
    """Return the sha256 digest of the complete core payload.

    The digest is computed from the canonical serialization produced by
    ``git_format.dumps_record`` (sorted keys at every level), so key
    insertion order never changes the result.
    """
    _require_mapping(core_payload, "core payload")
    _reject_nested_fields(core_payload, _SELF_DIGEST_FIELDS, "core payload")
    canonical = dumps_record(core_payload)
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(canonical).hexdigest()}"


def compute_report_digest(report):
    """Return the sha256 digest of a verification report.

    The full report shape is validated before any digest is computed:
    the report must be a dict with the expected schema and version,
    must reference a valid core digest, and must not carry its own
    digest. Anything else is rejected instead of digested.
    """
    _require_report_shape(report)
    canonical = dumps_record(report)
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(canonical).hexdigest()}"


def build_verification_report(core_payload, checks=None):
    """Build a verification report referencing the core payload's digest.

    ``checks`` is an optional caller-supplied JSON-safe dict of check
    results. The returned report contains no self-digest field; its
    digest is computed separately via :func:`compute_report_digest`.
    """
    core_digest = compute_core_digest(core_payload)
    if checks is None:
        checks = {}
    _require_mapping(checks, "checks")
    report = {
        "schema": REPORT_SCHEMA,
        "version": SCHEMA_VERSION,
        "core_digest": core_digest,
        "checks": dict(checks),
    }
    # Fail closed at construction time: the assembled report must pass
    # the full shape check (including the recursive cycle scan of
    # nested checks) and remain canonically serializable.
    compute_report_digest(report)
    return report


def _require_report_shape(report):
    _require_mapping(report, "verification report")
    _reject_nested_fields(report, _SELF_DIGEST_FIELDS, "verification report")
    if report.get("schema") != REPORT_SCHEMA:
        raise BackupManifestError("verification report has an unknown schema")
    if report.get("version") != SCHEMA_VERSION:
        raise BackupManifestError("verification report has an unsupported version")
    _require_digest(report.get("core_digest"), "verification report core_digest")


def build_final_manifest(core_payload, report):
    """Build the final manifest referencing the core and report digests.

    The manifest embeds the fixed core payload and records both the
    core digest and the report digest under ``verification``. It never
    records a digest of itself. The report's core reference is verified
    against the freshly computed core digest before the manifest is
    built.
    """
    _require_mapping(core_payload, "core payload")
    _require_report_shape(report)
    core_digest = compute_core_digest(core_payload)
    if report["core_digest"] != core_digest:
        raise BackupManifestError(
            "verification report references a different core digest"
        )
    report_digest = compute_report_digest(report)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "version": SCHEMA_VERSION,
        "core": core_payload,
        "verification": {
            "core_digest": core_digest,
            "report_digest": report_digest,
        },
    }
    return manifest


def verify_final_manifest(manifest, report):
    """Verify the whole chain, failing closed on any mismatch.

    Checks that the manifest and report have the expected shapes, that
    no self-referencing digest field exists, that the manifest's core
    payload reproduces the recorded core digest, that the report
    references the same core digest, and that the report reproduces the
    recorded report digest. Raises :class:`BackupManifestError` on any
    failure; returns ``None`` on success.
    """
    _require_mapping(manifest, "final manifest")
    # Recursive cycle scan: manifest_digest is circular at any depth,
    # and report_digest is legitimate only as the verification block's
    # direct reference to the verification report.
    _reject_nested_fields(
        manifest,
        _SELF_DIGEST_FIELDS,
        "final manifest",
        allowed_paths=_ALLOWED_MANIFEST_DIGEST_PATHS,
    )
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise BackupManifestError("final manifest has an unknown schema")
    if manifest.get("version") != SCHEMA_VERSION:
        raise BackupManifestError("final manifest has an unsupported version")
    if "core" not in manifest:
        raise BackupManifestError("final manifest carries no core payload")
    verification = manifest.get("verification")
    _require_mapping(verification, "final manifest verification block")
    _require_digest(verification.get("core_digest"), "manifest core_digest")
    _require_digest(verification.get("report_digest"), "manifest report_digest")

    _require_report_shape(report)
    core_digest = compute_core_digest(manifest["core"])
    if verification["core_digest"] != core_digest:
        raise BackupManifestError(
            "manifest core digest does not match the embedded core payload"
        )
    if report["core_digest"] != core_digest:
        raise BackupManifestError(
            "verification report references a different core digest"
        )
    report_digest = compute_report_digest(report)
    if verification["report_digest"] != report_digest:
        raise BackupManifestError(
            "manifest report digest does not match the verification report"
        )
    return None
