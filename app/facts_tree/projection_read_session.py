"""Owned read-only projection sessions and honest D5 readiness envelopes."""
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import sqlite3

from app.facts_tree import projection_router as router
from app.facts_tree.projection_bundle import BundleUnavailable, ProjectionBundle, open_bundle
from app.facts_tree.projection_status import (
    ProjectionStatus, ProjectionStatusError, build_projection_status,
)


REQUIRED_INDEXES = ("canonical_records", "wide_records", "fts_records")


@dataclass(frozen=True)
class ReadSession:
    serving: router.ServingGeneration
    standard: sqlite3.Connection
    derived: sqlite3.Connection | None
    bundle: ProjectionBundle | None
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def derived_ready(self) -> bool:
        return self.bundle is not None

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        if self.bundle is not None:
            self.bundle.close()
        else:
            self.standard.close()

    def __enter__(self) -> "ReadSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_read_session(root, *, repo_id: str, company_id: str,
                      allow_degraded: bool = True) -> ReadSession:
    """Prefer a validated pair; optionally fall back to a fresh standard handle."""
    if not isinstance(allow_degraded, bool):
        raise TypeError("allow_degraded must be bool")
    try:
        bundle = open_bundle(root, repo_id=repo_id, company_id=company_id)
    except BundleUnavailable:
        if not allow_degraded:
            raise
        serving, standard = router.open_current(root, repo_id=repo_id, company_id=company_id)
        with ExitStack() as cleanup:
            cleanup.callback(standard.close)
            session = ReadSession(serving, standard, None, None)
            cleanup.pop_all()
            return session
    with ExitStack() as cleanup:
        cleanup.callback(bundle.close)
        session = ReadSession(bundle.serving, bundle.standard, bundle.derived, bundle)
        cleanup.pop_all()
        return session


def session_status(session: ReadSession, head_commit: str, *,
                   snapshot_commit: str | None = None) -> ProjectionStatus:
    """Describe the pinned generation against caller-captured authoritative OIDs."""
    if not isinstance(session, ReadSession):
        raise ProjectionStatusError("session must be a ReadSession")
    available = REQUIRED_INDEXES if session.derived_ready else ("canonical_records",)
    view = replace(session.serving, required_indexes=available)
    return build_projection_status(head_commit, view, snapshot_commit=snapshot_commit,
                                   required_indexes=REQUIRED_INDEXES)
