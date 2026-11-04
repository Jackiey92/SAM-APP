"""E3b slice 3a: real-environment tests for ``verify_current_ledger``.

The strictly opt-in independent CURRENT host approval ledger narrow channel
(freeze revD §6.5). Every scenario here uses a real SQLite file built through
the real host service stack -- identity pairing, policy, task credential,
prepared effect, approval decision, reservation, consumption and the real
work-event store -- no mocks. The ledger always lives in its own directory,
strictly separated from the backup media directories.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.facts_tree.recovery_current_ledger import (
    CurrentLedgerError,
    verify_current_ledger,
)
from app.facts_tree.operation_work_store import OperationWorkStore
from app.facts_tree.recovery_epoch import EvidenceKey, EvidenceState
from app.host_approval_consumptions import HostApprovalConsumptionService
from tests.test_approval_use_e2 import stack
from tests.test_host_approval_consumptions import consume
from tests.test_host_approval_reservations import _connect, at
from tests.test_host_prepared_effects import (
    _time_text,
    effect_kwargs,
    scope_kwargs,
)

IDENT = {"repo_id": "repo-e3b", "installation_id": "inst-syn-1",
         "company_id": "co-syn-1"}
OBSERVED = at(100)


def ts(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


# -- real ledger builders ---------------------------------------------------------


def _key(cred, decision, effect):
    return EvidenceKey(cred.task_id, decision.approval_id, effect.operation_id)


def _open_host(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    host = stack(db)
    host.identity.initialize_installation(
        installation_id=IDENT["installation_id"],
        company_id=IDENT["company_id"],
        owner_principal_id="owner-e3b", owner_role="owner", now=at(0))
    pairing = host.identity.issue_pairing_credential(now=at(10),
                                                     lifetime_seconds=600)
    grant = host.identity.pair(pairing.secret, now=at(20),
                               session_lifetime_seconds=3600)
    policy = host.authorization.create_policy(grant.secret, now=at(30),
                                              **scope_kwargs())
    consumers = HostApprovalConsumptionService(db, host.reservations,
                                               initialize=True)
    return host, grant, policy, consumers, db


def _cred_effect(host, grant, policy):
    cred = host.credentials.issue_task_credential(
        grant.secret, now=at(40), policy_id=policy.policy_id,
        expected_revision=1)
    effect = host.prepared.prepare_effect(cred.secret, now=at(50),
                                          **effect_kwargs())
    return cred, effect


def _approve(host, grant, effect, *, decision="approved", expires=300):
    return host.decisions.approve_effect(
        grant.secret, now=at(60), operation_id=effect.operation_id,
        expected_effect_digest=effect.effect_digest,
        expected_preparation_digest=effect.preparation_digest,
        decision=decision, expires_at=at(expires))


def _reserve(host, cred, effect, decision):
    return host.reservations.reserve_approval(
        cred.secret, now=at(70), operation_id=effect.operation_id,
        approval_id=decision.approval_id,
        expected_effect_digest=effect.effect_digest,
        expected_preparation_digest=effect.preparation_digest)


def _chain(host, grant, policy, consumers, mode):
    cred, effect = _cred_effect(host, grant, policy)
    decision = _approve(host, grant, effect,
                        decision="denied" if mode == "denied" else "approved")
    reservation = None
    if mode in ("reserved", "revoked", "consumed"):
        reservation = _reserve(host, cred, effect, decision)
    if mode == "consumed":
        consume(consumers, cred, reservation)
    if mode == "revoked":
        host.credentials.revoke_task_credential(
            grant.secret, now=at(90), task_id=cred.task_id)
    return cred, effect, decision


def _work_unknown(db, task_id, operation_id):
    """Drive the real work store through its lifecycle to an unknown end."""
    store = OperationWorkStore(db)
    scope = (IDENT["repo_id"], IDENT["company_id"], operation_id)
    with db:
        db.execute("BEGIN")
        store.record_operation(*scope, task_id, "idem-unknown-" + operation_id[-6:],
                               _time_text(at(72)), {})
        store.mark_operation_prepared(*scope, "evt-u1-" + operation_id[-6:],
                                      _time_text(at(73)))
        store.mark_approval_required(*scope, "evt-u2-" + operation_id[-6:],
                                     _time_text(at(74)))
        store.mark_reserved(*scope, "evt-u3-" + operation_id[-6:],
                            _time_text(at(75)))
        store.record_attempt(*scope, "att-u1-" + operation_id[-6:],
                             _time_text(at(76)), "a" * 40, {}, {}, {},
                             object_format="sha1",
                             prepared_event_id="evt-u4-" + operation_id[-6:],
                             prepared_at=_time_text(at(76)))
        store.admit_execution(*scope, "att-u1-" + operation_id[-6:],
                              "evt-u5-" + operation_id[-6:],
                              "evt-u6-" + operation_id[-6:],
                              _time_text(at(77)))
        store.mark_unknown(*scope, "att-u1-" + operation_id[-6:],
                           "evt-u7-" + operation_id[-6:],
                           "evt-u8-" + operation_id[-6:],
                           _time_text(at(78)))


def build_ledger(db_path, mode="unused"):
    host, grant, policy, consumers, db = _open_host(db_path)
    cred, effect, decision = _chain(host, grant, policy, consumers, mode)
    db.close()
    _settle(db_path)
    return cred, effect, decision


def build_states_ledger(db_path):
    """One legal ledger carrying all five restrictive states plus unused."""
    host, grant, policy, consumers, db = _open_host(db_path)
    keys = {}
    for mode in ("consumed", "revoked", "denied", "reserved", "unused"):
        cred, effect, decision = _chain(host, grant, policy, consumers, mode)
        keys[mode] = _key(cred, decision, effect)
    cred, effect = _cred_effect(host, grant, policy)
    _work_unknown(db, cred.task_id, effect.operation_id)
    keys["unknown"] = EvidenceKey(cred.task_id, "had-work-unknown",
                                  effect.operation_id)
    db.close()
    _settle(db_path)
    return keys


def build_empty_ledger(db_path):
    """Four required tables exist through real DDL; zero approval rows."""
    _host, _grant, _policy, _consumers, db = _open_host(db_path)
    db.close()
    _settle(db_path)


def _settle(path, moment=OBSERVED):
    ns = int(moment.timestamp() * 1_000_000_000)
    os.utime(path, ns=(ns, ns))


def copy_ledger(src, dst):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    _settle(dst)
    return dst


def rebuild_ledger(src, dst):
    """Logical copy: identical approval rows, deliberately different bytes."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with _connect(src) as source:
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
    with _connect(dst) as target:
        target.execute("PRAGMA page_size=%d"
                       % (8192 if page_size == 4096 else 4096))
        with _connect(src) as source:
            for statement in source.iterdump():
                target.execute(statement)
    _settle(dst)
    return dst


# -- call helpers -----------------------------------------------------------------


def verify(path, keys, **overrides):
    kwargs = dict(ledger_path=os.fspath(path), expected_identity=dict(IDENT),
                  keys=tuple(keys), observed_at=ts(OBSERVED),
                  now=ts(OBSERVED), trust_anchor_id="ta-e3b")
    kwargs.update(overrides)
    return verify_current_ledger(**kwargs)


def expect_reject(path, keys, code, **overrides):
    with pytest.raises(CurrentLedgerError) as caught:
        verify(path, keys, **overrides)
    assert str(caught.value) == code
    text = str(caught.value) + repr(caught.value.args)
    assert os.fspath(path) not in text
    assert "SELECT" not in text and "sql" not in text.lower()
    return caught.value


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def fingerprint_tree(root):
    root = Path(root)
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        entries.append((str(rel_dir), "dir",
                        tuple(sorted(dirnames)), tuple(sorted(filenames))))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            st = full.stat()
            entries.append((str(rel_dir / name), "file", st.st_size,
                            sha256_file(full)))
    return tuple(entries)


def current_path(tmp_path, name="app.db"):
    path = tmp_path / "current" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def tamper(path, attack):
    with _connect(path) as db:
        if attack == "blob":
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='host_approval_decisions_no_update'").fetchone()[0]
            db.execute("DROP TRIGGER host_approval_decisions_no_update")
            db.execute(
                "UPDATE host_approval_decisions "
                "SET record_blob=record_blob||x'78'")
            db.execute(ddl)
        elif attack == "predecessor":
            guards = db.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger'").fetchall()
            ddls = [(name, sql) for name, sql in guards
                    if "host_prepared_effects" in sql]
            for name, _ in ddls:
                db.execute("DROP TRIGGER " + name)
            db.execute("DELETE FROM host_prepared_effects")
            for _, sql in ddls:
                db.execute(sql)
        db.commit()
    _settle(path)


# ===========================================================================
# Green paths
# ===========================================================================


def test_g1_unused_approved_unexpired_key_proves_unused(tmp_path):
    path = current_path(tmp_path)
    cred, effect, decision = build_ledger(path, mode="unused")
    key = _key(cred, decision, effect)
    other = EvidenceKey("htk-absent-task", "had-absent", "hop-absent")
    proof = verify(path, [key, other])
    assert proof.unused_keys == frozenset({key})
    assert proof.restricted == ()
    assert proof.trust_anchor_id == "ta-e3b"
    assert proof.repo_id == IDENT["repo_id"]
    assert proof.installation_id == IDENT["installation_id"]
    assert proof.company_id == IDENT["company_id"]
    assert proof.observed_at == ts(OBSERVED)


def test_g2_all_restrictive_states_are_real_and_sorted(tmp_path):
    path = current_path(tmp_path)
    keys = build_states_ledger(path)
    scrambled = [keys["unknown"], keys["reserved"], keys["denied"],
                 keys["revoked"], keys["consumed"], keys["unused"]]
    proof = verify(path, scrambled)
    assert proof.unused_keys == frozenset({keys["unused"]})
    expected_order = (
        (keys["consumed"], EvidenceState.CONSUMED.value),
        (keys["revoked"], EvidenceState.REVOKED.value),
        (keys["denied"], EvidenceState.POLICY_RESTRICTED.value),
        (keys["reserved"], EvidenceState.RESERVED.value),
        (keys["unknown"], EvidenceState.UNKNOWN.value),
    )
    assert proof.restricted == expected_order


def test_g3_anchors_and_watermark_shape_stability_and_rows(tmp_path):
    first = current_path(tmp_path)
    build_ledger(first, mode="unused")
    proof = verify(first, [EvidenceKey("t", "a", "o")])
    assert len(proof.anchors) == 3
    assert proof.anchors[0].startswith("current-file:sha256:")
    inode = proof.anchors[1]
    assert inode.startswith("current-inode:")
    dev, ino, mtime_ns = inode.split(":", 1)[1].split(":")
    assert int(dev) and int(ino) and int(mtime_ns) >= 0
    assert proof.anchors[2].startswith("current-watermark:")
    count, digest = proof.watermark
    assert count == 2  # one prepared-effect row + one decision row
    assert int(digest, 16) and len(digest) == 64
    assert proof.anchors[2] == "current-watermark:%d:%s" % (count, digest)

    # Identical bytes at another independent path yield the same watermark.
    copy = tmp_path / "sibling" / "app-copy.db"
    copy_ledger(first, copy)
    proof_copy = verify(copy, [EvidenceKey("t", "a", "o")])
    assert proof_copy.watermark == proof.watermark
    assert proof_copy.anchors[0] == proof.anchors[0]

    # Row count moves the count and the digest.
    consumed = tmp_path / "consumed" / "app.db"
    consumed.parent.mkdir(parents=True)
    build_ledger(consumed, mode="consumed")
    proof_consumed = verify(consumed, [EvidenceKey("t", "a", "o")])
    assert proof_consumed.watermark[0] == 4
    assert proof_consumed.watermark != proof.watermark


# ===========================================================================
# Rejection paths
# ===========================================================================


def test_r1_missing_and_zero_byte_ledger_unreachable(tmp_path):
    expect_reject(tmp_path / "missing" / "app.db",
                  [EvidenceKey("t", "a", "o")],
                  "current_ledger_unreachable")
    empty = current_path(tmp_path, "empty.db")
    empty.write_bytes(b"")
    expect_reject(empty, [EvidenceKey("t", "a", "o")],
                  "current_ledger_unreachable")


@pytest.mark.parametrize("placement", ["equal", "nested"])
def test_r2_ledger_inside_backup_media_rejected(tmp_path, placement):
    src = current_path(tmp_path)
    build_ledger(src, mode="unused")
    media = tmp_path / "media"
    if placement == "equal":
        inside = media / "app.db"
    else:
        inside = media / "nested" / "deep" / "app.db"
    copy_ledger(src, inside)
    expect_reject(inside, [EvidenceKey("t", "a", "o")],
                  "current_ledger_within_backup_media",
                  media_roots=(os.fspath(media),))


def test_r2_sibling_outside_media_is_not_rejected(tmp_path):
    path = current_path(tmp_path)
    cred, effect, decision = build_ledger(path, mode="unused")
    media = tmp_path / "media"
    media.mkdir()
    (media / "w_items.sqlite").write_bytes(b"backup package bytes")
    proof = verify(path, [_key(cred, decision, effect)],
                   media_roots=(os.fspath(media),))
    assert proof.unused_keys == frozenset({_key(cred, decision, effect)})


def test_r3_byte_digest_in_backup_digests_rejected(tmp_path):
    path = current_path(tmp_path)
    build_ledger(path, mode="unused")
    expect_reject(path, [EvidenceKey("t", "a", "o")],
                  "current_ledger_backup_copy",
                  backup_file_digests=frozenset({sha256_file(path)}))


def test_r9_backup_w_items_copy_cannot_serve_as_current_ledger(tmp_path):
    src = current_path(tmp_path)
    cred, effect, decision = build_ledger(src, mode="unused")
    backup_dir = tmp_path / "backup-package" / "payload" / "w"
    backup_dir.mkdir(parents=True)
    backup_file = backup_dir / "w_items.sqlite"
    shutil.copyfile(src, backup_file)
    candidate = tmp_path / "elsewhere" / "app.db"
    copy_ledger(backup_file, candidate)
    expect_reject(candidate, [_key(cred, decision, effect)],
                  "current_ledger_backup_copy",
                  media_roots=(os.fspath(tmp_path / "backup-package"),),
                  backup_file_digests=frozenset({sha256_file(backup_file)}))
    # A genuinely different ledger (different bytes, same approval rows) next
    # to it is still independently valid.
    other = tmp_path / "elsewhere" / "other.db"
    rebuild_ledger(src, other)
    assert sha256_file(other) != sha256_file(backup_file)
    proof = verify(other, [_key(cred, decision, effect)],
                   media_roots=(os.fspath(tmp_path / "backup-package"),),
                   backup_file_digests=frozenset({sha256_file(backup_file)}))
    assert _key(cred, decision, effect) in proof.unused_keys


def test_r4_old_observation_rejected(tmp_path):
    path = current_path(tmp_path)
    build_ledger(path, mode="unused")
    expect_reject(path, [EvidenceKey("t", "a", "o")],
                  "current_ledger_observation_stale",
                  observed_at=ts(at(100)), now=ts(at(400)),
                  max_age_seconds=120)


def test_r4_old_snapshot_file_rejected(tmp_path):
    path = current_path(tmp_path)
    build_ledger(path, mode="unused")
    # File mtime stays at 100; observation claims 400 with a tight window.
    expect_reject(path, [EvidenceKey("t", "a", "o")],
                  "current_ledger_stale_snapshot",
                  observed_at=ts(at(400)), now=ts(at(400)),
                  max_age_seconds=120)


@pytest.mark.parametrize("table", [
    "host_prepared_effects", "host_approval_decisions",
    "host_approval_reservations", "host_approval_consumptions"])
def test_r5_missing_required_table_rejected(tmp_path, table):
    path = current_path(tmp_path)
    build_ledger(path, mode="unused")
    with _connect(path) as db:
        db.execute("DROP TABLE " + table)
        db.commit()
    _settle(path)
    expect_reject(path, [EvidenceKey("t", "a", "o")],
                  "current_ledger_tables_incomplete")


@pytest.mark.parametrize("attack", ["blob", "predecessor"])
def test_r6_broken_chain_rejected(tmp_path, attack):
    path = current_path(tmp_path)
    cred, effect, decision = build_ledger(path, mode="reserved")
    tamper(path, attack)
    expect_reject(path, [_key(cred, decision, effect)],
                  "current_ledger_chain_invalid")


def test_r7_tables_present_but_zero_rows_rejected(tmp_path):
    path = current_path(tmp_path)
    build_empty_ledger(path)
    expect_reject(path, [EvidenceKey("t", "a", "o")],
                  "current_ledger_no_rows")


@pytest.mark.parametrize("kwargs, code", [
    (dict(keys=[EvidenceKey("t", "a", "o")]),
     "current_ledger_input_invalid"),
    (dict(keys=()), "current_ledger_input_invalid"),
    (dict(keys=(EvidenceKey("t", "a", "o"), EvidenceKey("t", "a", "o"))),
     "current_ledger_input_invalid"),
    (dict(keys=(object(),)), "current_ledger_input_invalid"),
    (dict(max_age_seconds=0), "current_ledger_input_invalid"),
    (dict(max_age_seconds=86401), "current_ledger_input_invalid"),
    (dict(max_age_seconds=1.5), "current_ledger_input_invalid"),
    (dict(max_age_seconds="120"), "current_ledger_input_invalid"),
    (dict(max_age_seconds=True), "current_ledger_input_invalid"),
    (dict(ledger_path="/nonexistent\x00/app.db"),
     "current_ledger_input_invalid"),
    (dict(backup_file_digests=frozenset({"sha256:" + "z" * 64})),
     "current_ledger_input_invalid"),
    (dict(expected_identity={"installation_id": "i", "company_id": "c"}),
     "current_ledger_identity_invalid"),
    (dict(expected_identity={"repo_id": "r", "installation_id": "",
                             "company_id": "c"}),
     "current_ledger_identity_invalid"),
    (dict(expected_identity="not-a-dict"),
     "current_ledger_identity_invalid"),
    (dict(trust_anchor_id=""), "current_ledger_trust_anchor_invalid"),
    (dict(trust_anchor_id=None), "current_ledger_trust_anchor_invalid"),
])
def test_r8_invalid_inputs_raise_fixed_codes(tmp_path, kwargs, code):
    valid = dict(ledger_path=os.fspath(current_path(tmp_path)),
                 expected_identity=dict(IDENT),
                 keys=(EvidenceKey("t", "a", "o"),), observed_at=ts(OBSERVED),
                 now=ts(OBSERVED), trust_anchor_id="ta-e3b")
    valid.update(kwargs)
    with pytest.raises(CurrentLedgerError) as caught:
        verify_current_ledger(**valid)
    assert str(caught.value) == code


# ===========================================================================
# Read-only discipline: neither success nor rejection writes anything.
# ===========================================================================


def test_verifier_is_strictly_read_only(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "w_items.sqlite").write_bytes(b"backup package bytes")

    green = current_path(tmp_path)
    cred, effect, decision = build_ledger(green, mode="unused")
    key = _key(cred, decision, effect)

    tampered = tmp_path / "tampered" / "app.db"
    tampered.parent.mkdir()
    cred2, effect2, decision2 = build_ledger(tampered, mode="reserved")
    tamper(tampered, "blob")

    empty = tmp_path / "empty-ledger" / "app.db"
    empty.parent.mkdir()
    build_empty_ledger(empty)

    dropped = tmp_path / "dropped" / "app.db"
    dropped.parent.mkdir()
    build_ledger(dropped, mode="unused")
    with _connect(dropped) as db:
        db.execute("DROP TABLE host_approval_consumptions")
        db.commit()
    _settle(dropped)

    zero = tmp_path / "zero" / "app.db"
    zero.parent.mkdir()
    zero.write_bytes(b"")

    inside = tmp_path / "media" / "inside" / "app.db"
    copy_ledger(green, inside)

    cases = [
        (green, [key], dict(media_roots=(os.fspath(media),)), True),
        (green, [key], dict(backup_file_digests=frozenset(
            {sha256_file(media / "w_items.sqlite")})), True),
        (tampered, [_key(cred2, decision2, effect2)], {}, False),
        (empty, [EvidenceKey("t", "a", "o")], {}, False),
        (dropped, [EvidenceKey("t", "a", "o")], {}, False),
        (zero, [EvidenceKey("t", "a", "o")], {}, False),
        (inside, [key], dict(media_roots=(os.fspath(media),)), False),
        (green, [key], dict(observed_at=ts(at(100)), now=ts(at(400))), False),
        (green, [key], dict(observed_at=ts(at(400)), now=ts(at(400))), False),
    ]
    for path, keys, kwargs, should_succeed in cases:
        roots = {os.fspath(Path(path).parent), os.fspath(tmp_path),
                 os.fspath(media)}
        before = {root: fingerprint_tree(root) for root in roots}
        raised = None
        try:
            verify(path, keys, **kwargs)
        except CurrentLedgerError as exc:
            raised = exc
        after = {root: fingerprint_tree(root) for root in roots}
        assert before == after, path
        if should_succeed:
            assert raised is None, path
        else:
            assert type(raised) is CurrentLedgerError, path


# ===========================================================================
# P0-1: triple attribution -- a key may not borrow another approval's chain
# ===========================================================================


def test_p01_grafted_approval_id_cannot_borrow_an_unused_chain(tmp_path):
    path = current_path(tmp_path)
    keys = build_states_ledger(path)
    # Consumed approval A's id grafted onto unrelated unused approval B's
    # (task, operation): the live B chain must not read A's id as unused.
    graft = EvidenceKey(keys["unused"].task_id,
                        keys["consumed"].approval_id,
                        keys["unused"].operation_id)
    proof = verify(path, [graft, keys["consumed"], keys["unused"]])
    assert graft not in proof.unused_keys
    # Non-attribution is fail-closed: absent from the proof, so the decision
    # boundary maps it to independent_current_proof_incomplete, never
    # openable (its full triple matches neither a chain nor any tombstone).
    assert proof.state_for(graft) is None
    # The real triples keep their exact states (no over-tightening).
    assert keys["unused"] in proof.unused_keys
    assert proof.state_for(keys["consumed"]) == EvidenceState.CONSUMED


def test_p02_grafted_triples_in_every_direction_never_read_as_unused(tmp_path):
    path = current_path(tmp_path)
    keys = build_states_ledger(path)
    grafts = (
        # Consumed task + unused (approval, op).
        EvidenceKey(keys["consumed"].task_id, keys["unused"].approval_id,
                    keys["unused"].operation_id),
        # Unused (task, approval) + consumed op.
        EvidenceKey(keys["unused"].task_id, keys["unused"].approval_id,
                    keys["consumed"].operation_id),
        # Unused (task, op) + a reserved approval id.
        EvidenceKey(keys["unused"].task_id, keys["reserved"].approval_id,
                    keys["unused"].operation_id),
    )
    proof = verify(path, [*grafts, keys["unused"], keys["consumed"],
                          keys["reserved"]])
    assert proof.unused_keys == frozenset({keys["unused"]})
    for graft in grafts:
        assert graft not in proof.unused_keys
        assert proof.state_for(graft) is None
    assert proof.state_for(keys["consumed"]) == EvidenceState.CONSUMED
    assert proof.state_for(keys["reserved"]) == EvidenceState.RESERVED


def test_p03_legitimate_six_state_triples_keep_g2_attribution(tmp_path):
    path = current_path(tmp_path)
    keys = build_states_ledger(path)
    scrambled = [keys["unknown"], keys["reserved"], keys["denied"],
                 keys["revoked"], keys["consumed"], keys["unused"]]
    proof = verify(path, scrambled)
    assert proof.unused_keys == frozenset({keys["unused"]})
    expected_order = (
        (keys["consumed"], EvidenceState.CONSUMED.value),
        (keys["revoked"], EvidenceState.REVOKED.value),
        (keys["denied"], EvidenceState.POLICY_RESTRICTED.value),
        (keys["reserved"], EvidenceState.RESERVED.value),
        (keys["unknown"], EvidenceState.UNKNOWN.value),
    )
    assert proof.restricted == expected_order
    expected_state = (
        ("consumed", EvidenceState.CONSUMED),
        ("revoked", EvidenceState.REVOKED),
        ("denied", EvidenceState.POLICY_RESTRICTED),
        ("reserved", EvidenceState.RESERVED),
        ("unknown", EvidenceState.UNKNOWN),
    )
    for name, state in expected_state:
        assert proof.state_for(keys[name]) == state
    assert proof.state_for(keys["unused"]) == EvidenceState.UNUSED
