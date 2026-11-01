"""Pure business-tree validation for the Broker final gate (design 96 §5.6).

Slice 4 will call this before CAS, using physical bytes already read by the
host and its bound repository identity. FactsGitRepository.accept does not
call it. This slice builds no acceptance workflow, idempotency or recovery.
No Git objects, refs, files, network or processes are accessed here.
"""
from collections.abc import Mapping

from app.facts_tree import record_tree
from app.facts_tree.record_tree import ValidatedRecordTree


class BrokerTreeGateError(ValueError):
    """The complete supplied tree failed validation; no partial result exists."""


def validate_broker_tree(content_by_path: Mapping[str, bytes], *, repo_id,
                         company_id, commit_oid, head_oid,
                         object_format="sha1") -> ValidatedRecordTree:
    """Validate the supplied exact bytes with registered schemas/closures 1–5.

    The host supplies trusted scope and commit/head context. This pure function
    preserves byte object identity, never reads storage, and leaves input
    untouched. Validation errors retain their diagnostic codes and cause.
    """
    try:
        if not isinstance(content_by_path, Mapping):
            raise TypeError("content_by_path must be a mapping")
        return record_tree.build_record_tree(
            content_by_path.items(), repo_id=repo_id, company_id=company_id,
            commit_oid=commit_oid, head_oid=head_oid, object_format=object_format)
    except (TypeError, ValueError) as exc:
        raise BrokerTreeGateError(
            "Broker full record tree failed validation: %s" % exc) from exc
