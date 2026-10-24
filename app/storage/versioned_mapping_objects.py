"""Immutable content-addressed byte objects for exact historical mappings.

Low-level F-001 R-closure primitive: regenerating the latest mapping can never
replace an exact historical version, so historical mapping bytes are kept as
immutable objects addressed by their SHA-256 digest.  Callers must inject an
explicit storage root; nothing here reads environment variables, default
``data/`` paths, or the application database.  Objects are created without
overwriting, re-validated on every read, and invalid digests, missing objects,
and content mismatches all fail closed.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class InvalidDigestError(ValueError):
    """The supplied address is not a lowercase SHA-256 hex digest."""


class ObjectMissingError(KeyError):
    """No object is stored under the requested digest."""


class ObjectMismatchError(ValueError):
    """Stored bytes do not match their content address; never auto-repaired."""


def _validate_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise InvalidDigestError("digest must be a lowercase sha256 hex digest")
    return digest


class VersionedMappingObjectStore:
    """Create-only SHA-256-addressed byte objects under an explicit root."""

    def __init__(self, root: Path | str) -> None:
        if root is None or not str(root):
            raise ValueError("an explicit storage root is required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest_of(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _path_for(self, digest: str) -> Path:
        _validate_digest(digest)
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        """Store ``data`` under its digest; replaying the same bytes is a no-op.

        A pre-existing address is kept only when its content matches; a
        mismatched occupant raises and is never repaired or replaced.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        data = bytes(data)
        digest = self.digest_of(data)
        path = self.root / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)

        # A unique temporary plus a create-only link keep concurrent writers
        # from ever replacing an immutable object at the same address.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".object-",
                                             suffix=".tmp", delete=False) as stream:
                tmp_path = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(tmp_path, path)
            except FileExistsError:
                self._verify(path, digest)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> bytes:
        """Return the exact bytes for ``digest`` after re-validating content."""
        path = self._path_for(digest)
        if not path.is_file():
            raise ObjectMissingError(digest)
        return self._verify(path, digest)

    def has(self, digest: str) -> bool:
        """True only when the object exists and still matches its digest."""
        path = self._path_for(digest)
        if not path.is_file():
            return False
        self._verify(path, digest)
        return True

    @staticmethod
    def _verify(path: Path, digest: str) -> bytes:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ObjectMismatchError(f"sha256 mismatch for object {digest}")
        return data
