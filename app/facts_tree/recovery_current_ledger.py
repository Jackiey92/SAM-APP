"""E3b §6.5: one real, strictly opt-in independent CURRENT ledger verifier.

The recovery admission B route ("unused at backup time, proven independently
now") needs a current host approval ledger reached through a trust anchor
independent of the disaster backup media. This module is the single real
implementation of that observation. Nothing in production wiring calls it:
the only callers are tests and the future, separately approved I slice. When
nobody supplies the resulting ``CurrentLedgerProof`` to the decision boundary,
the B route stays closed (``needs_evidence``).

Independence follows D's out-of-band handoff trust model (freeze §6.5 allows
exactly that): the ledger parent is pinned NOFOLLOW/ancestor-walked, must be a
directory distinct from and not nested inside the backup package, the staged
bare or the recovery state root, and the observed bytes must not equal any
backup W file byte-for-byte (a copied ``w_items.sqlite`` is refused even from
another path). Shape checks, row SHA and pinned paths do not by themselves
prove independence: the caller is the trusted host boundary that attests
``trust_anchor_id`` and the connection-authenticated ``observed_at``, the same
attestation relationship as D's handoff and the owner recovery authentication.

Current-time/replay discipline: full enumeration of every ledger row produces
a complete-coverage watermark; ``observed_at`` must be within the freshness
window of ``now`` and the pinned file's own mtime must fall in that same
window, so an old snapshot replayed later with a fresh timestamp is refused.
An empty ledger is refused outright; a ledger with rows that do not cover a
key yields a proof without that key, which the decision boundary treats as
``needs_evidence`` (it never reads as "unused").

Read-only, offline: a single immutable-mode SQLite open, no writes, no
network, no subprocess, no model. All failures carry fixed codes only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
import stat

from app.facts_tree.recovery_approval_decision import (
    AdmissionDecisionError, CurrentLedgerProof,
)
from app.facts_tree.recovery_epoch import EvidenceKey, EvidenceState
from app.facts_tree.recovery_verifier import (
    _approval_snapshot, _file, _hash, _hex, _physical, _root, _sqlite,
)

_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%f+00:00"
_ROLE = "current"
_TABLE_RECORD = {
    "host_approval_consumptions": EvidenceState.CONSUMED,
    "host_approval_reservations": EvidenceState.RESERVED,
}


class CurrentLedgerError(AdmissionDecisionError):
    """Fixed reason code; never includes paths, SQL or ledger contents."""


def _label(value, code):
    if type(value) is not str or not value:
        raise CurrentLedgerError(code)


def _time(db, value, code):
    # Reuse the exact approval timestamp discipline (32 chars, normalized
    # through SQLite's calendar so a hostile string cannot smuggle a skew).
    from app.facts_tree.recovery_verifier import _approval_time
    try:
        return _approval_time(db, value)
    except Exception as exc:
        raise CurrentLedgerError(code) from exc


def _parse(value):
    return datetime.strptime(value, _TIME_FMT).replace(tzinfo=timezone.utc)


def _render(value):
    return value.strftime(_TIME_FMT)


def _pin_media_roots(paths):
    roots = []
    try:
        for path in paths:
            fd = _root(path)
            info = os.fstat(fd)
            roots.append((fd, (info.st_dev, info.st_ino)))
        return roots
    except (OSError, ValueError, TypeError) as exc:
        for fd, _ in roots:
            os.close(fd)
        raise CurrentLedgerError("current_ledger_media_root_invalid") from exc


def _ancestor_identities(fd):
    current = os.dup(fd)
    ancestors = []
    try:
        while True:
            info = os.fstat(current)
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                             | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = parent
            parent_info = os.fstat(current)
            if (parent_info.st_dev, parent_info.st_ino) == (info.st_dev, info.st_ino):
                break
            ancestors.append((parent_info.st_dev, parent_info.st_ino))
    finally:
        os.close(current)
    return tuple(ancestors)


def verify_current_ledger(*, ledger_path, expected_identity, keys, observed_at,
        now, trust_anchor_id, media_roots=(), backup_file_digests=frozenset(),
        max_age_seconds=120):
    """Observe one independent current ledger; fail closed with fixed codes.

    Returned ``CurrentLedgerProof`` covers only keys with complete current
    evidence: restrictive chains map to hard states, an approved/unreserved/
    unconsumed chain whose approval is unexpired at ``observed_at`` maps to
    unused; everything else is simply absent from the proof.
    """
    ledger_path = os.fspath(ledger_path)
    if (type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400
            or not isinstance(ledger_path, str) or "\x00" in ledger_path):
        raise CurrentLedgerError("current_ledger_input_invalid")
    directory, name = os.path.split(os.path.abspath(ledger_path))
    if not name or name in (".", "..") or "/" in name:
        raise CurrentLedgerError("current_ledger_input_invalid")
    for field in ("repo_id", "installation_id", "company_id"):
        _label(expected_identity.get(field) if type(expected_identity) is dict
               else None, "current_ledger_identity_invalid")
    if (type(keys) is not tuple or not keys
            or any(type(k) is not EvidenceKey for k in keys)):
        raise CurrentLedgerError("current_ledger_input_invalid")
    if len(set(keys)) != len(keys):
        raise CurrentLedgerError("current_ledger_input_invalid")
    _label(trust_anchor_id, "current_ledger_trust_anchor_invalid")
    if type(backup_file_digests) is not frozenset or any(
            type(d) is not str or not d.startswith("sha256:")
            or not _hex(d[7:], 64) for d in backup_file_digests):
        raise CurrentLedgerError("current_ledger_input_invalid")

    media = ()
    root_fd = ledger_fd = db = None
    try:
        try:
            root_fd = _root(directory)
            ledger_fd = _file(root_fd, name)
        except (OSError, ValueError) as exc:
            raise CurrentLedgerError("current_ledger_unreachable") from exc
        stat_info = os.fstat(ledger_fd)
        if not stat.S_ISREG(stat_info.st_mode) or stat_info.st_size == 0:
            raise CurrentLedgerError("current_ledger_unreachable")
        own_identity = (stat_info.st_dev, os.fstat(root_fd).st_ino)

        media = _pin_media_roots(tuple(media_roots))
        media_identities = tuple(identity for _, identity in media)
        ancestors = _ancestor_identities(root_fd)
        if own_identity in media_identities or any(
                identity in ancestors for identity in media_identities):
            raise CurrentLedgerError("current_ledger_within_backup_media")

        file_digest, _size = _hash(ledger_fd)
        if file_digest in backup_file_digests:
            raise CurrentLedgerError("current_ledger_backup_copy")

        try:
            db = _sqlite(ledger_fd)
            tables = _physical(db)
        except Exception as exc:
            raise CurrentLedgerError("current_ledger_storage_invalid") from exc
        observed = _time(db, observed_at, "current_ledger_observation_invalid")
        current = _time(db, now, "current_ledger_observation_invalid")
        try:
            observed_dt, now_dt = _parse(observed), _parse(current)
            window = timedelta(seconds=max_age_seconds)
            if not (now_dt - window <= observed_dt <= now_dt + timedelta(seconds=5)):
                raise CurrentLedgerError("current_ledger_observation_stale")
            mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
            if not (observed_dt - window <= mtime <= observed_dt + timedelta(seconds=5)):
                raise CurrentLedgerError("current_ledger_stale_snapshot")
        except CurrentLedgerError:
            raise
        except (ValueError, OverflowError, OSError) as exc:
            raise CurrentLedgerError("current_ledger_observation_invalid") from exc

        required = frozenset((
            "host_prepared_effects", "host_approval_decisions",
            "host_approval_reservations", "host_approval_consumptions"))
        if not required <= tables:
            raise CurrentLedgerError("current_ledger_tables_incomplete")

        snapshot = dict(_approval_snapshot(
            {"identity": expected_identity,
             "recovery_capabilities": {"approval_consumptions":
                                       {"version": 1, "roles": [_ROLE]}},
             "payload": {"files": []}},
            [(_ROLE, db, tables)]))
        failures = tuple(snapshot["failures"])
        if failures:
            raise CurrentLedgerError("current_ledger_chain_invalid")
        records = snapshot["records"]
        if not records:
            raise CurrentLedgerError("current_ledger_no_rows")

        # Every state is attributed to the requested FULL (task_id,
        # approval_id, operation_id) triple. The approval rows carry all three
        # coordinates in their hash-bound payloads, so a key that borrows one
        # approval's id with another task/op matches no row and stays absent
        # from the proof; operation-only memberships must never attribute a
        # different approval's chain (design I1: switching operation rejects).
        approval_tables = frozenset((
            "host_approval_decisions", "host_approval_reservations",
            "host_approval_consumptions"))
        by_table = {table: {} for table in required}
        triples = {table: set() for table in approval_tables}
        for raw_entry in records:
            entry = dict(raw_entry)
            record = dict(entry["record"])
            op = record.get("operation_id")
            if type(op) is str and op:
                by_table[entry["table"]][op] = record
            if entry["table"] in approval_tables:
                triple = (record.get("task_id"), record.get("approval_id"), op)
                if all(type(part) is str and part for part in triple):
                    triples[entry["table"]].add(triple)
        decisions = by_table["host_approval_decisions"]
        consumed_ops = set(snapshot["consumption_operations"])
        consumed_triples = triples["host_approval_consumptions"]
        reserved_triples = triples["host_approval_reservations"]
        decision_triples = triples["host_approval_decisions"]
        revoked_tasks = set(snapshot["revoked_tasks"])
        # Old work facts have no approval coordinate (no decision exists for
        # them); their tightest honest attribution is (task_id, operation_id).
        unknown_work = set()
        for raw_row in snapshot["restrictive_rows"]:
            row = dict(raw_row)
            if (row.get("state") == "unknown"
                    and row.get("table") == "fact_operation_work_events"
                    and type(row.get("task_id")) is str and row["task_id"]
                    and type(row.get("operation_id")) is str
                    and row["operation_id"]):
                unknown_work.add((row["task_id"], row["operation_id"]))
        chain_ops = set(snapshot["chain_operations"])

        unused, restricted = set(), []
        for key in keys:
            state = None
            triple = (key.task_id, key.approval_id, key.operation_id)
            if triple in consumed_triples and key.operation_id in consumed_ops:
                state = EvidenceState.CONSUMED
            elif key.task_id in revoked_tasks:
                state = EvidenceState.REVOKED
            elif (key.task_id, key.operation_id) in unknown_work:
                state = EvidenceState.UNKNOWN
            elif triple in reserved_triples:
                state = EvidenceState.RESERVED
            elif triple in decision_triples and key.operation_id in chain_ops:
                decision = decisions[key.operation_id]
                if decision["decision"] == "denied":
                    state = EvidenceState.POLICY_RESTRICTED
                elif (decision["decision"] == "approved"
                        and decision["created_at"] <= observed
                        and decision["expires_at"] > observed):
                    state = EvidenceState.UNUSED
            if state is None:
                continue
            if state is EvidenceState.UNUSED:
                unused.add(key)
            else:
                restricted.append((key, state.value))
        order = {EvidenceState.CONSUMED: 0, EvidenceState.REVOKED: 1,
                 EvidenceState.POLICY_RESTRICTED: 2, EvidenceState.RESERVED: 3,
                 EvidenceState.UNKNOWN: 4}
        restricted.sort(key=lambda pair: (order[EvidenceState(pair[1])],
                                          pair[0].task_id, pair[0].approval_id,
                                          pair[0].operation_id))

        watermark_entries = [dict(entry) for entry in records]
        watermark_input = "\n".join(
            entry["table"] + ":" + entry["row_sha256"] for entry in sorted(
                watermark_entries,
                key=lambda item: (item["table"], item["row_sha256"])))
        watermark_digest = hashlib.sha256(watermark_input.encode("ascii")).hexdigest()
        anchors = (
            "current-file:" + file_digest,
            "current-inode:%d:%d:%d" % (stat_info.st_dev, stat_info.st_ino,
                                        stat_info.st_mtime_ns),
            "current-watermark:%d:%s" % (len(records), watermark_digest),
        )
        return CurrentLedgerProof(
            trust_anchor_id=trust_anchor_id, observed_at=observed,
            repo_id=expected_identity["repo_id"],
            installation_id=expected_identity["installation_id"],
            company_id=expected_identity["company_id"],
            watermark=(len(records), watermark_digest),
            unused_keys=frozenset(unused), restricted=tuple(restricted),
            anchors=anchors)
    finally:
        if db is not None:
            db.close()
        if ledger_fd is not None:
            os.close(ledger_fd)
        if root_fd is not None:
            os.close(root_fd)
        for fd, _ in media:
            os.close(fd)
