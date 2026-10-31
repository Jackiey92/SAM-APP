"""D15-1 real Git drills; E3 ends at content APIs, E5 is component-only replay."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.facts_tree import git_format, projection_advance_service as service
from app.facts_tree import projection_derived_query as derived, projection_query as query
from app.facts_tree import projection_read_session as sessions, projection_router as router
from app.facts_tree.git_repository import FactsGitRepository, GitRepositoryError
from app.facts_tree.operation_acceptance import locate_operation_acceptance
from app.facts_tree.operation_work_store import OperationWorkStore
from app.facts_tree.record_schema import SCHEMA_COLUMNS
from tests.facts_tree_full_fixture import full_tree
from tests.test_projection_derived_advance import _contents
from tests.test_projection_incremental import _append


STAMP = "2026-11-01T00:00:00+00:00"
AUTHZ = "d15-isolated-host"
META = git_format.record_path("repository_meta")


def path(family, **identity):
    return git_format.record_path(family, **identity)


def ref(family, uid, revision):
    return {"record_type": family, "stable_id": uid, "revision": revision}


def scoped_tree(scope, base):
    tree = full_tree()

    def scope_values(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in scope:
                    value[key] = scope[key]
                else:
                    scope_values(item)
        elif isinstance(value, list):
            for item in value:
                scope_values(item)

    scope_values(tree)
    for payload in tree.values():
        if payload["record_type"] in ("operation", "event"):
            payload["base_commit"] = base
    source = tree[path("source_version", source_id="src-1", version_id="opaque-version")]
    tree[path("evidence", evidence_id="evidence-1")]["quote_digest"] = sha256(
        git_format.dumps_record(source)).hexdigest()
    issue = tree[path("issue_event", issue_uid="issue-1", event_id="issue-event-1")]
    tree[path("event", event_id="event-1")]["after"] = [sha256(git_format.dumps_record(issue)).hexdigest()]
    return tree


@pytest.fixture
def scene(tmp_path, request):
    # Scope isolation is independent of temporary-directory isolation.
    scope = {"repo_id": "repo-d15-" + request.node.name,
             "company_id": "acc-d15-" + request.node.name}
    repo = FactsGitRepository.initialize(tmp_path / "facts.git", installation_id="install-d15", **scope)
    genesis = repo.head_oid()
    tree = scoped_tree(scope, genesis)
    tree[META] = git_format.loads_record(repo.read_snapshot_at(genesis).records[0].content)
    commits = [genesis, _append(repo, tree)]
    current = tree[path("fact_current", fact_uid="fact-1")]
    current["status"] = "retracted"
    commits.append(_append(repo, tree))
    current["status"] = "active"
    commits.append(_append(repo, tree))
    query.set_cursor_key(b"test-only-D4-host-signing-key")
    return SimpleNamespace(repo=repo, tree=tree, scope=scope, commits=commits,
                           root=tmp_path / "root", directory=tmp_path / "generations")


def pointer(scene):
    return scene.root / git_format.encode_id(scene.scope["repo_id"]) / git_format.encode_id(
        scene.scope["company_id"]) / "current.json"


def observe(scene):
    """Read only public sessions at publication boundaries; reject half pairs."""
    try:
        session = sessions.open_read_session(scene.root, **scene.scope, allow_degraded=False)
    except router.NoCurrentProjection:
        return None
    with session:
        serving = session.serving
        assert serving.projected_commit in scene.repo.list_commit_chain(scene.repo.zero_oid)
        identity = {"generation_id": serving.generation_id, "projected_commit": serving.projected_commit,
                    "render_sha256": serving.render_sha256, **scene.scope, "status": "ready"}
        for connection, table in ((session.standard, "generation_meta"),
                                  (session.derived, "wide_meta"), (session.derived, "fts_meta")):
            meta = dict(connection.execute("SELECT key,value FROM " + table))
            assert {key: meta[key] for key in identity} == identity
        assert session.standard.execute("SELECT count(*) FROM record_canonical").fetchone()[0] == serving.record_count
        assert session.derived.execute("SELECT count(*) FROM dw_fact_current").fetchone()[0] == (
            session.standard.execute("SELECT count(*) FROM fact_current_view").fetchone()[0])
        return serving.projected_commit


def advance(scene, *, observations=None):
    def now():
        if observations is not None:
            observations.append(observe(scene))
        return STAMP

    report = service.advance_projection(scene.repo, scene.root, scene.directory,
        expected_repo_id=scene.scope["repo_id"], expected_company_id=scene.scope["company_id"], now=now)
    assert observe(scene) == scene.repo.head_oid()
    return report


def contents(scene):
    with sessions.open_read_session(scene.root, **scene.scope, allow_degraded=False) as session:
        canonical = {table: session.standard.execute(
            "SELECT * FROM " + table + " ORDER BY 1").fetchall()
            for table in ("record_canonical", "generation_meta", "family_counts",
                          "fact_current_view", "placement_current_view")}
        companion = _contents(session.derived)
        assert set(companion["wide"]) == set(SCHEMA_COLUMNS)
        page = query.list_records(session.standard, authz_digest=AUTHZ)
        assert page.records and page.record_count == session.serving.record_count
        assert derived.list_family_rows(session.bundle, "fact_revision", authz_digest=AUTHZ).rows
        assert derived.search(session.bundle, "示例", family="fact_revision", authz_digest=AUTHZ).hits
        return session.serving, canonical, companion


def inventory(directory):
    return {p.name: sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}


def assert_noop(scene):
    before = inventory(scene.directory)
    report = advance(scene)
    assert report.already_current and report.published_commits == () and report.generations == ()
    assert report.from_commit == report.to_commit == scene.repo.head_oid()
    assert inventory(scene.directory) == before


def add_operation(scene, tree, suffix, *, terminal, fact_uid=None):
    """Extend the complete legal fixture, including explicit current/placement outputs."""
    template = scoped_tree(scene.scope, scene.repo.head_oid())
    candidate_uid, operation_id, event_id = "candidate-" + suffix, "operation-" + suffix, "event-" + suffix
    input_record = deepcopy(template[path("input_candidate", candidate_uid="candidate-一", candidate_revision=1)])
    input_record["candidate_uid"] = candidate_uid
    candidate = {"candidate_uid": candidate_uid, "candidate_revision": 1,
                 "payload_digest": input_record["payload_digest"]}
    outputs = []
    if fact_uid is None:
        issue_uid, issue_event = "issue-" + suffix, "issue-event-" + suffix
        output_ref = ref("issue_event", issue_uid, issue_event)
        issue = deepcopy(template[path("issue_event", issue_uid="issue-1", event_id="issue-event-1")])
        issue.update(issue_uid=issue_uid, event_id=issue_event, status="resolved" if terminal else "open",
                     disposition_result={"terminal": terminal, "result_refs": [output_ref]})
        tree[path("issue_event", issue_uid=issue_uid, event_id=issue_event)] = issue
        outputs.append(output_ref)
        after = [sha256(git_format.dumps_record(issue)).hexdigest()]
    else:
        assert terminal
        fact = deepcopy(template[path("fact_revision", fact_uid="fact-1", revision=1)])
        fact["fact_uid"] = fact_uid
        tree[path("fact_revision", fact_uid=fact_uid, revision=1)] = fact
        current = deepcopy(template[path("fact_current", fact_uid="fact-1")])
        current.update(fact_uid=fact_uid, revision=1)
        tree[path("fact_current", fact_uid=fact_uid)] = current
        placement = deepcopy(template[path("placement_revision", fact_uid="fact-1", revision=1)])
        placement.update(fact_uid=fact_uid, fact_ref=ref("fact_revision", fact_uid, 1))
        tree[path("placement_revision", fact_uid=fact_uid, revision=1)] = placement
        current_placement = deepcopy(template[path("placement_current", fact_uid="fact-1")])
        current_placement["fact_uid"] = fact_uid
        tree[path("placement_current", fact_uid=fact_uid)] = current_placement
        outputs = [ref("fact_revision", fact_uid, 1), ref("placement_revision", fact_uid, 1)]
        after = [1, 1]
    result = {"terminal": terminal, "result_refs": outputs}
    operation = deepcopy(template[path("operation", operation_id="operation-1")])
    operation.update(operation_id=operation_id, request_id="request-" + suffix,
        candidate_refs=[candidate], disposition_result=[{**candidate, **deepcopy(result)}],
        formal_fact_refs=[outputs[0]] if fact_uid else [], event_ids=[event_id])
    event = deepcopy(template[path("event", event_id="event-1")])
    event.update(event_id=event_id, operation_id=operation_id, changed_refs=outputs,
                 affected_refs=deepcopy(outputs), before=[None] * len(outputs), after=after)
    tree[path("input_candidate", candidate_uid=candidate_uid, candidate_revision=1)] = input_record
    tree[path("operation", operation_id=operation_id)] = operation
    tree[path("event", event_id=event_id)] = event
    if terminal:
        consumption = deepcopy(template[path("consumed_candidate", candidate_uid="candidate-一", candidate_revision=1)])
        consumption.update(candidate_uid=candidate_uid, operation_id=operation_id, disposition_result=deepcopy(result))
        tree[path("consumed_candidate", candidate_uid=candidate_uid, candidate_revision=1)] = consumption
    return candidate_uid, operation_id


def test_e1_loss_and_explicit_dangling_pointer_recovery(scene):
    """Both loss modes rebuild through advance; dangling-pointer removal is explicit ops recovery."""
    advance(scene)
    baseline = contents(scene)
    for loss in ("all-including-pointer", "current-pair-only"):
        if loss == "all-including-pointer":
            shutil.rmtree(scene.directory)
            pointer(scene).unlink()
            with pytest.raises(router.NoCurrentProjection):
                sessions.open_read_session(scene.root, **scene.scope)
        else:
            serving = baseline[0]
            Path(serving.db_path).unlink()
            (scene.directory / ("derived-" + serving.generation_id + ".sqlite")).unlink()
            assert list(scene.directory.glob("gen-*.sqlite"))  # Surviving older generations must not win.
            for read in (router.load_current, sessions.open_read_session):
                with pytest.raises(router.CurrentProjectionUnavailable):
                    read(scene.root, **scene.scope)
            with pytest.raises(service.AdvanceServiceError) as error:
                advance(scene)
            assert isinstance(error.value.__cause__, router.CurrentProjectionUnavailable)
            # R1: explicit operator invalidation, never a production recovery API.
            pointer(scene).unlink()
        observations = []
        report = advance(scene, observations=observations)
        assert not report.already_current and report.to_commit == scene.repo.head_oid()
        assert report.published_commits == scene.repo.list_commit_chain(scene.repo.zero_oid)
        assert observations and None in observations
        assert contents(scene) == baseline
        assert_noop(scene)


def test_e2_rebuilt_projection_does_not_release_consumed_candidate(scene):
    candidate_uid, operation_id = add_operation(scene, scene.tree, "consumed", terminal=True, fact_uid="fact-consumed")
    accepted = _append(scene.repo, scene.tree)
    advance(scene)
    baseline = contents(scene)
    consumed_path = path("consumed_candidate", candidate_uid=candidate_uid, candidate_revision=1)
    before = {r.path: r.content for r in scene.repo.read_snapshot_at(accepted).records}
    shutil.rmtree(scene.directory)
    pointer(scene).unlink()
    scene.repo = FactsGitRepository.open(scene.repo.path, installation_id="install-d15", **scene.scope)
    advance(scene)
    after = {r.path: r.content for r in scene.repo.read_snapshot_at(accepted).records}
    assert after == before and git_format.loads_record(after[consumed_path])["operation_id"] == operation_id
    assert contents(scene) == baseline
    # A new operation attempting to re-consume this candidate must pass the real Git gate.
    duplicate = deepcopy(scene.tree)
    other_uid, other_operation = add_operation(scene, duplicate, "duplicate", terminal=True, fact_uid="fact-duplicate")
    del duplicate[path("input_candidate", candidate_uid=other_uid, candidate_revision=1)]
    operation = duplicate[path("operation", operation_id=other_operation)]
    operation["candidate_refs"][0]["candidate_uid"] = candidate_uid
    operation["disposition_result"][0]["candidate_uid"] = candidate_uid
    consumption = duplicate.pop(path("consumed_candidate", candidate_uid=other_uid, candidate_revision=1))
    consumption["candidate_uid"] = candidate_uid
    duplicate[consumed_path] = consumption
    tree_oid = scene.repo.build_tree([(p, git_format.dumps_record(v)) for p, v in duplicate.items()],
                                     expected_paths=list(duplicate))
    attempted = scene.repo.create_commit(tree_oid, accepted)
    with pytest.raises(GitRepositoryError):
        scene.repo.accept(accepted, attempted, expected_paths=list(duplicate))
    assert scene.repo.head_oid() == accepted
    assert contents(scene) == baseline
    assert_noop(scene)


def test_e3_qualifiers_forecast_decimal_end_at_content_apis_no_cloud_export(scene):
    """Coverage boundary: D14 query -> cloud snapshot wiring is unbuilt; stop at both content APIs."""
    add_operation(scene, scene.tree, "qualified", terminal=True, fact_uid="fact-qualified")
    fact_path = path("fact_revision", fact_uid="fact-qualified", revision=1)
    fact = scene.tree[fact_path]
    amount = Decimal("123456789.00100")
    fact.update(statement_kind="forecast", statement_raw="预计到账；尚未收款\n未收款原文",
                value_type="number", value_raw="123456789.00100", canonical_value=amount)
    for key, state in zip(("period", "currency", "scope", "basis"),
                          ("not_stated", "not_applicable", "ambiguous", "known")):
        fact["qualifiers"][key].update(status=state, text="尚未收款：" + state,
                                      value="未收款" if state == "known" else None)
    accepted = _append(scene.repo, scene.tree)
    advance(scene)
    expected = git_format.dumps_record(fact)
    snapshot = {r.path: r.content for r in scene.repo.read_snapshot_at(accepted).records}
    assert snapshot[fact_path] == expected
    with sessions.open_read_session(scene.root, **scene.scope) as session:
        page = derived.list_family_rows(session.bundle, "fact_revision", authz_digest=AUTHZ)
        row = next(row for row in page.rows if row.path == fact_path)
        raw, family, checksum, size = query.get_record_canonical(session.standard, fact_path, authz_digest=AUTHZ)
        assert raw == expected and family == "fact_revision"
        assert checksum == row.canonical_sha256 == sha256(expected).hexdigest()
        assert size == row.byte_length == len(expected)
        assert "123456789.00100" in row.payload_json and b"123456789.00100" in raw
        for text in (row.payload_json, raw):
            actual = git_format.loads_record(text)
            assert actual == fact
            assert actual["statement_kind"] == "forecast" and actual["statement_raw"] == fact["statement_raw"]
            assert actual["canonical_value"].as_tuple() == amount.as_tuple()
            assert actual["qualifiers"] == fact["qualifiers"]


def test_e4_pending_and_conflict_keep_current_counts_success_adds_one(scene):
    advance(scene)

    def current_rows():
        with sessions.open_read_session(scene.root, **scene.scope) as session:
            wide = session.derived.execute("SELECT fact_uid,revision,status FROM dw_fact_current ORDER BY fact_uid").fetchall()
            canonical = query.list_records(session.standard, family="fact_current", authz_digest=AUTHZ).records
            heads = session.standard.execute("SELECT * FROM fact_current_view ORDER BY fact_uid").fetchall()
            assert len(wide) == len(canonical) == len(heads)
            return wide, canonical, heads

    baseline = current_rows()
    for suffix, terminal in (("pending", False), ("conflict", True)):
        add_operation(scene, scene.tree, suffix, terminal=terminal)
        _append(scene.repo, scene.tree)
        advance(scene)
        assert current_rows() == baseline
    add_operation(scene, scene.tree, "success", terminal=True, fact_uid="fact-success")
    _append(scene.repo, scene.tree)
    advance(scene)
    current = current_rows()
    assert all(len(new) == len(old) + 1 for new, old in zip(current, baseline))
    assert ("fact-success", 1, "active") in current[0]
    assert ("fact-success", 1, 1) in current[2]


def test_e5_receipt_loss_component_replay_no_public_replay_entry(scene, tmp_path):
    """降级：底层零件组合，无对外重放入口；OWS accepted_commit is only a mirror."""
    candidate_uid, operation_id = add_operation(scene, scene.tree, "receipt-loss", terminal=True, fact_uid="fact-receipt-loss")
    base = scene.repo.head_oid()
    request = {"candidate_uid": candidate_uid, "intent": "accept once"}
    plan, effect, deps = {"paths": sorted(scene.tree)}, {"fact_uid": "fact-receipt-loss"}, {"base": base}
    operation = scene.tree[path("operation", operation_id=operation_id)]
    operation["request_digest"] = sha256(git_format.dumps_record(request)).hexdigest()
    operation["effect_digest"] = sha256(git_format.dumps_record(effect)).hexdigest()
    scope = (scene.scope["repo_id"], scene.scope["company_id"], operation_id)
    attempt_id = "attempt-receipt-loss"
    work_path = tmp_path / "operation-work.sqlite"
    with closing(sqlite3.connect(work_path)) as conn:
        store = OperationWorkStore(conn)
        with conn:
            conn.execute("BEGIN")
            recorded = store.record_operation(*scope, "task-d15", "key-d15", STAMP, request)
            assert recorded["request_sha256"] == operation["request_digest"]
            store.mark_operation_prepared(*scope, "op-prepared", STAMP)
            attempt = store.record_attempt(*scope, attempt_id, STAMP, base, plan, effect, deps,
                object_format=scene.repo.object_format, prepared_event_id="attempt-prepared", prepared_at=STAMP)
            assert attempt["effect_sha256"] == operation["effect_digest"]
            store.admit_execution(*scope, attempt_id, "op-inflight", "attempt-accepting", STAMP)
    tree_oid = scene.repo.build_tree([(p, git_format.dumps_record(v)) for p, v in scene.tree.items()],
                                     expected_paths=list(scene.tree))
    accepted = scene.repo.create_commit(tree_oid, base)
    scene.repo.accept(base, accepted, expected_paths=list(scene.tree))  # Result deliberately lost.
    scene.repo = FactsGitRepository.open(scene.repo.path, installation_id="install-d15", **scene.scope)
    assert scene.repo.head_oid() == accepted
    proof = locate_operation_acceptance(scene.repo, operation_id=operation_id, commit_oid=accepted)
    assert proof.accepted_commit_oid == accepted
    with closing(sqlite3.connect(work_path)) as conn:
        store = OperationWorkStore(conn)
        assert store.get_operation(*scope)["accepted_commit"] is None
        with conn:
            conn.execute("BEGIN")
            for replay_number in range(2):
                assert store.record_operation(*scope, "task-d15", "key-d15", STAMP, request)["replayed"]
                assert store.record_attempt(*scope, attempt_id, STAMP, base, plan, effect, deps,
                    object_format=scene.repo.object_format, prepared_event_id="attempt-prepared", prepared_at=STAMP)["replayed"]
                pair = store.mark_accepted(*scope, attempt_id, proof.accepted_commit_oid,
                                           "op-accepted", "attempt-accepted", STAMP)
                assert pair["operation_event"]["replayed"] == bool(replay_number)
                assert pair["attempt_event"]["replayed"] == bool(replay_number)
            assert store.get_operation(*scope)["accepted_commit"] == accepted
            assert store.get_attempt(*scope, attempt_id)["accepted_commit"] == accepted
            assert len(store.list_operation_events(*scope)) == len(store.list_attempt_events(*scope, attempt_id)) == 3
            assert len(store.list_attempts(*scope)) == 1
            assert store.verify_integrity() is None
    retry = scene.repo.accept(base, accepted, expected_paths=list(scene.tree))
    assert not retry.accepted and retry.current_oid == scene.repo.head_oid() == accepted
    advance(scene)
    baseline = contents(scene)
    assert_noop(scene)
    assert contents(scene) == baseline
    records = scene.repo.read_snapshot_at(accepted).records
    consumed_path = path("consumed_candidate", candidate_uid=candidate_uid, candidate_revision=1)
    assert sum(record.path == consumed_path for record in records) == 1
    with sessions.open_read_session(scene.root, **scene.scope) as session:
        assert session.derived.execute("SELECT count(*) FROM dw_fact_current WHERE fact_uid=?",
                                       ("fact-receipt-loss",)).fetchone() == (1,)
        assert session.standard.execute("SELECT count(*) FROM fact_current_view WHERE fact_uid=?",
                                        ("fact-receipt-loss",)).fetchone() == (1,)
