"""S1 source registry + legacy credential backfill: real isolated SQLite only.

No model calls, no credentials, no real company database. Every company here is
an ``acc-*`` temporary identity inside a pytest tmp_path ledger.
"""
import json
import sqlite3

import pytest

from app import settings
from app.db.database import connect, init_db
from app.facts_tree.source_backfill import (
    REASON_BAD_COORDINATE,
    REASON_MISSING_SOURCE,
    REASON_MISSING_SOURCE_FILE,
    REASON_OUTSIDE_COMPANY,
    SourceBackfill,
)
from app.facts_tree.source_registry import (
    RUNTIME_TABLES,
    SourceRegistry,
    SourceRejected,
)
from tests.memory_setup import migrated_memory
from tests.test_facts_tree_provider import Embedding
from tests.test_facts_tree_leaves import COMPANY, OTHER, SHA

COMPANY_B = 'acc-src-b'
MISSING = 'c' * 64

FACT_COLUMNS = ('company_id,entity,attribute,period,value,value_type,unit,'
                'source_file,source_span,valid_from,valid_to,confidence,status,'
                'confirm_mode,module_key,created_at')


def _span(locator='docx:body/1/paragraph', quote='注册资本900万元',
          ranges=((0, 7),), field_evidence=None, protocol=3, fingerprint='mf-good'):
    span = {'source_loc': locator, 'quote': quote, 'ranges': [list(r) for r in ranges],
            'mapped_fingerprint': fingerprint, 'reading_protocol': protocol}
    if field_evidence is not None:
        span['field_evidence'] = field_evidence
    return json.dumps(span, ensure_ascii=False)


def _proof(field='registered_capital', quote='900万元', ranges=((10, 15),),
           file_hash=SHA, grant_id='grant-1'):
    return {'field': field, 'quote': quote, 'ranges': [list(r) for r in ranges],
            'mapped_fingerprint': 'mf-proof', 'source_loc': 'docx:table/1/cell/2',
            'grant_id': grant_id, 'file_hash': file_hash}


class Ledger:
    def __init__(self, path, conn):
        self.path = path
        self.conn = conn


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    path = tmp_path / 'ledger.db'
    memory = migrated_memory(path, embedding=Embedding(), company_id=COMPANY)
    conn = connect(path)
    try:
        yield Ledger(path, conn)
    finally:
        conn.close()
        memory.close()


def put_file(conn, sha, name, *, companies=(), zone='internal'):
    conn.execute(
        'INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,origin_zone,uploaded_at) '
        'VALUES(?,?,?,?,?,?)', (sha, name, 1, '/unused', zone, 'now'))
    for company in companies:
        conn.execute(
            'INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,'
            'harness_format,size_bytes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (f'job-{company}-{sha[:8]}', company, sha, name, 'docx', 'md', 1, 'now', 'now'))


def put_fact(conn, company, *, source_file=SHA, source_span=_span(), status='verified',
             superseded_by=None, valid_to=None, entity='临时主体', attribute='注册资本',
             value='900'):
    cur = conn.execute(
        f'INSERT INTO facts({FACT_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (company, entity, attribute, '2025-01', value, 'number', '万元', source_file,
         source_span, None, valid_to, 1.0, status, 'manual', 'bizov', 'now'))
    if superseded_by is not None:
        conn.execute('UPDATE facts SET superseded_by=? WHERE id=?', (superseded_by, cur.lastrowid))
    return cur.lastrowid


def table_counts(conn):
    return {name: conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
            for name in RUNTIME_TABLES}


def snapshot_facts(conn):
    return [tuple(row) for row in conn.execute('SELECT * FROM facts ORDER BY id').fetchall()]


# --- group 1: registration, determinism, replay ----------------------------

def test_register_source_version_evidence_link_and_replay_zero_duplicates(ledger):
    db = ledger.conn
    put_file(db, SHA, 'sample.docx', companies=(COMPANY,))
    fact_id = put_fact(db, COMPANY)
    db.commit()

    registry = SourceRegistry(ledger.path, company_id=COMPANY)
    first = registry.register_file_source(file_hash=SHA, original_name='sample.docx')
    assert first['source_created'] and first['version_created']
    again = registry.register_file_source(file_hash=SHA)
    assert not again['source_created'] and not again['version_created']
    assert first['source_id'] == again['source_id'] and first['version_id'] == again['version_id']
    assert first['source_id'] != SHA

    ev1 = registry.add_evidence(file_hash=SHA, locator='docx:body/1/paragraph',
                                quote='注册资本900万元', coordinate={'ranges': [[0, 7]]})
    ev1_replay = registry.add_evidence(file_hash=SHA, locator='docx:body/1/paragraph',
                                       quote='注册资本900万元', coordinate={'ranges': [[0, 7]]})
    assert ev1['evidence_id'] == ev1_replay['evidence_id'] and not ev1_replay['created']
    ev2 = registry.add_evidence(file_hash=SHA, locator='docx:table/1/cell/2',
                                quote='900万元', coordinate={'ranges': [[10, 15]]})
    assert ev2['created'] and ev2['evidence_id'] != ev1['evidence_id']

    link = registry.link_fact(fact_id=fact_id, evidence_id=ev1['evidence_id'])
    assert link['created'] and link['status'] == 'active' and link['relation'] == 'supports'
    link_replay = registry.link_fact(fact_id=fact_id, evidence_id=ev1['evidence_id'])
    assert not link_replay['created'] and link_replay['fact_fingerprint'] == link['fact_fingerprint']

    counts = table_counts(db)
    assert (counts['sources'], counts['source_versions'], counts['source_evidence'],
            counts['fact_evidence_links']) == (1, 1, 2, 1)
    # source + version + 2 evidence + 1 link = 5 audit events; replay appends nothing.
    assert counts['source_events'] == 5
    registry.close()


def test_evidence_rejects_unknown_and_malformed_inputs(ledger):
    db = ledger.conn
    put_file(db, SHA, 'sample.docx', companies=(COMPANY,))
    db.commit()
    registry = SourceRegistry(ledger.path, company_id=COMPANY)
    with pytest.raises(SourceRejected):
        SourceRegistry(ledger.path, company_id='acc-bad\ncompany')
    with pytest.raises(SourceRejected):
        registry.register_file_source(file_hash='not-a-hash')
    with pytest.raises(SourceRejected):
        registry.add_evidence(file_hash=SHA, locator='x', quote='q', coordinate=[])
    with pytest.raises(SourceRejected):
        registry.link_fact(fact_id=1, evidence_id='evd_forged', relation='endorse')
    registry.close()


# --- group 2: one source / many evidences; company independence; forgery ----

def test_one_source_many_evidences_cross_company_independence_and_forgery(ledger):
    db = ledger.conn
    put_file(db, SHA, 'shared.docx', companies=(COMPANY, COMPANY_B))
    fact_a = put_fact(db, COMPANY)
    fact_b = put_fact(db, COMPANY_B, entity='B主体')
    db.commit()

    reg_a = SourceRegistry(ledger.path, company_id=COMPANY)
    reg_b = SourceRegistry(ledger.path, company_id=COMPANY_B)
    a = reg_a.register_file_source(file_hash=SHA)
    b = reg_b.register_file_source(file_hash=SHA)
    assert a['source_id'] != b['source_id'] and a['version_id'] != b['version_id']

    made = None
    for locator, quote in (('l1', 'q1'), ('l2', 'q2'), ('l3', 'q3')):
        made = reg_a.add_evidence(file_hash=SHA, locator=locator, quote=quote)
        assert made['created']
    assert db.execute(
        "SELECT count(*) FROM sources WHERE company_id=? AND origin_key=?",
        (COMPANY, SHA)).fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM source_versions').fetchone()[0] == 2

    # Cross-company id forgery: ids are company-derived and resolve nowhere.
    with pytest.raises(SourceRejected, match='outside this company'):
        reg_b.add_evidence(source_id=a['source_id'], version_id=a['version_id'],
                           locator='l', quote='q')
    with pytest.raises(SourceRejected, match='outside the host company scope'):
        reg_b.link_fact(fact_id=fact_b, evidence_id='evd_' + 'f' * 40)
    with pytest.raises(SourceRejected, match='outside the host company scope'):
        reg_b.link_fact(fact_id=fact_a, evidence_id='evd_x')
    with pytest.raises(SourceRejected, match='outside the host company scope'):
        reg_a.links_for_fact(fact_b)
    reg_a.link_fact(fact_id=fact_a, evidence_id=made['evidence_id'])
    assert reg_b.links_for_fact(fact_b) == []
    assert reg_b.evidence_for_fact(fact_b) == []
    assert reg_b.distinct_source_ids_for_fact(fact_b) == []
    assert reg_a.distinct_source_ids_for_fact(fact_a) == [a['source_id']]
    # Forgery attempts created nothing on either side.
    assert db.execute('SELECT count(*) FROM fact_evidence_links').fetchone()[0] == 1
    reg_a.close()
    reg_b.close()


def test_registering_unscoped_hash_is_hard_rejected(ledger):
    db = ledger.conn
    put_file(db, OTHER, 'foreign.docx', companies=(COMPANY_B,))
    db.commit()
    registry = SourceRegistry(ledger.path, company_id=COMPANY)
    with pytest.raises(SourceRejected, match='outside the host company scope'):
        registry.register_file_source(file_hash=OTHER)
    assert table_counts(db)['sources'] == 0
    registry.close()


# --- group 3: backfill taxonomy, supersession, quarantine, byte-unchanged ---

def _seed_backfill_world(ledger):
    """Nine company facts covering every quarantine class plus full history."""
    conn = ledger.conn
    put_file(conn, SHA, 'good.docx', companies=(COMPANY,))
    put_file(conn, OTHER, 'foreign.docx', companies=(COMPANY_B,))
    good_span = _span(field_evidence=[
        _proof(),
        {'field': 'bad', 'quote': 'no coordinates'},
        _proof(file_hash=OTHER),
    ])
    f1 = put_fact(conn, COMPANY, source_span=good_span)
    f2 = put_fact(conn, COMPANY, attribute='旧版注册资本', value='800',
                  source_span=_span(quote='注册资本800万元', fingerprint='mf-old'))
    f_new = put_fact(conn, COMPANY, source_span=_span())
    conn.execute('UPDATE facts SET superseded_by=? WHERE id=?', (f_new, f2))
    conn.execute('UPDATE facts SET valid_to=? WHERE id=?', ('2024-12-31', f2))
    f_null = put_fact(conn, COMPANY, attribute='无源', source_file=None, source_span=None)
    f_outside = put_fact(conn, COMPANY, attribute='越公司', source_file=OTHER,
                         source_span=_span())
    f_nospan = put_fact(conn, COMPANY, attribute='无坐标', source_span=None)
    f_garbage = put_fact(conn, COMPANY, attribute='坏JSON', source_span='{not json')
    f_badranges = put_fact(conn, COMPANY, attribute='坏坐标',
                           source_span=json.dumps({'source_loc': 'x', 'quote': 'q',
                                                   'mapped_fingerprint': 'mf', 'ranges': []}))
    conn.commit()
    # Orphaned legacy fact: source_file points at a hash with no source_files row.
    # FK enforcement is per-connection, mirroring a pre-FK legacy ledger.
    orphan = connect(ledger.path)
    try:
        orphan.execute('PRAGMA foreign_keys=OFF')
        cur = orphan.execute(
            f'INSERT INTO facts({FACT_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (COMPANY, '临时主体', '鬼影文件', '2025-01', '1', 'number', '份', MISSING,
             _span(), None, None, 1.0, 'verified', 'manual', 'bizov', 'now'))
        f_missing = cur.lastrowid
        orphan.commit()
    finally:
        orphan.close()
    return dict(f1=f1, f2=f2, f_new=f_new, f_null=f_null, f_missing=f_missing,
                f_outside=f_outside, f_nospan=f_nospan, f_garbage=f_garbage,
                f_badranges=f_badranges)


def test_backfill_quarantine_taxonomy_superseded_and_rerun_resolution(ledger):
    db = ledger.conn
    ids = _seed_backfill_world(ledger)
    before = snapshot_facts(db)

    backfill = SourceBackfill(ledger.path, company_id=COMPANY)
    receipt = backfill.run(request_sha='acceptance-round-1')
    counts = receipt['counts']
    assert counts['scanned'] == 9
    assert sorted(receipt['linked_fact_ids']) == sorted([ids['f1'], ids['f2'], ids['f_new']])
    assert counts['linked_facts'] == 3
    assert counts['quarantined'] == 6
    assert counts['by_reason'] == {
        REASON_MISSING_SOURCE: 1,
        REASON_MISSING_SOURCE_FILE: 1,
        REASON_OUTSIDE_COMPANY: 1,
        REASON_BAD_COORDINATE: 3,
    }
    # f1 carries a valid field proof plus one malformed and one foreign-file
    # proof; f_new reuses f1's exact primary coordinate, so evidence is shared.
    assert counts['evidence_new'] == 3  # f1 primary + f1 proof + f2 primary
    assert counts['links_new'] == 4     # one link per (fact, evidence)
    assert counts['sources_new'] == counts['versions_new'] == 1
    assert counts['skipped_field_proofs'] == 2

    assert [row[0] for row in db.execute(
        'SELECT DISTINCT status FROM fact_evidence_links')] == ['legacy_backfilled']
    basis = [json.loads(row[0]) for row in db.execute(
        'SELECT basis FROM fact_evidence_links').fetchall()]
    assert all(not b['verified'] and not b['two_point_confirmation'] for b in basis)

    open_q = {(row['fact_id'], row['reason'], row['resolved_at'])
              for row in backfill.open_quarantine()}
    assert open_q == {
        (ids['f_null'], REASON_MISSING_SOURCE, None),
        (ids['f_missing'], REASON_MISSING_SOURCE_FILE, None),
        (ids['f_outside'], REASON_OUTSIDE_COMPANY, None),
        (ids['f_nospan'], REASON_BAD_COORDINATE, None),
        (ids['f_garbage'], REASON_BAD_COORDINATE, None),
        (ids['f_badranges'], REASON_BAD_COORDINATE, None),
    }
    # Old fact rows are byte-identical; batch receipt is stored.
    assert snapshot_facts(db) == before
    stored = backfill.batch(receipt['batch_id'])
    assert stored['finished_at'] and json.loads(stored['result'])['counts'] == counts

    # Same request replays the stored receipt with zero writes.
    snapshot = table_counts(db)
    replay = backfill.run(request_sha='acceptance-round-1')
    assert replay['replay'] is True and replay['counts'] == counts
    assert table_counts(db) == snapshot

    # Repair the ledger (test-side mutation), then a NEW request resolves entries.
    db.execute(
        'INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,'
        'harness_format,size_bytes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        ('job-fixed', COMPANY, OTHER, 'foreign.docx', 'docx', 'md', 1, 'now', 'now'))
    db.execute('UPDATE facts SET source_span=? WHERE id=?',
               (_span(quote='越公司文件坐标'), ids['f_outside']))
    db.execute('UPDATE facts SET source_span=? WHERE id=?',
               (_span(quote='无坐标修复后的独立坐标'), ids['f_nospan']))
    db.commit()
    second = backfill.run(request_sha='acceptance-round-2')
    assert second['counts']['scanned'] == 9
    assert second['counts']['linked_facts'] == 5
    assert second['counts']['quarantined'] == 4
    assert second['counts']['resolved'] == 2
    assert second['counts']['sources_new'] == 1
    assert second['counts']['versions_new'] == 1
    assert second['counts']['evidence_new'] == 2
    assert second['counts']['links_new'] == 2
    open_ids = {row['fact_id'] for row in backfill.open_quarantine()}
    assert open_ids == {ids['f_null'], ids['f_missing'], ids['f_garbage'], ids['f_badranges']}
    resolved = db.execute(
        'SELECT count(*) FROM source_backfill_quarantine '
        'WHERE fact_id IN (?,?) AND resolved_at IS NOT NULL AND resolved_batch_id=?',
        (ids['f_outside'], ids['f_nospan'], second['batch_id'])).fetchone()[0]
    assert resolved == 2
    backfill.close()


def test_backfill_is_single_company_scoped(ledger):
    db = ledger.conn
    put_file(db, SHA, 'good.docx', companies=(COMPANY, COMPANY_B))
    put_fact(db, COMPANY, source_span=_span())
    put_fact(db, COMPANY_B, entity='B主体', source_span=_span())
    db.commit()
    backfill = SourceBackfill(ledger.path, company_id=COMPANY)
    receipt = backfill.run(request_sha='company-a-only')
    assert receipt['counts']['scanned'] == 1
    assert db.execute(
        'SELECT count(*) FROM sources WHERE company_id=?', (COMPANY_B,)).fetchone()[0] == 0
    backfill.close()


# --- group 4: append-only immutability and company-scoped reads -------------

@pytest.mark.parametrize('table', ['sources', 'source_versions', 'source_evidence', 'source_events'])
def test_registry_tables_are_append_only(ledger, table):
    db = ledger.conn
    put_file(db, SHA, 'good.docx', companies=(COMPANY,))
    put_fact(db, COMPANY)
    db.commit()
    backfill = SourceBackfill(ledger.path, company_id=COMPANY)
    backfill.run(request_sha='immutable')
    with pytest.raises(sqlite3.Error):
        db.execute(f'UPDATE {table} SET rowid=rowid')
    with pytest.raises(sqlite3.Error):
        db.execute(f'DELETE FROM {table}')
    db.rollback()
    assert table_counts(db)[table] >= 1
    backfill.close()


def test_quarantine_resolution_is_mutable_but_links_keep_legacy_status(ledger):
    db = ledger.conn
    put_file(db, SHA, 'good.docx', companies=(COMPANY,))
    put_fact(db, COMPANY, attribute='无坐标', source_span=None)
    db.commit()
    backfill = SourceBackfill(ledger.path, company_id=COMPANY)
    backfill.run(request_sha='q1')
    db.execute('UPDATE source_backfill_quarantine SET resolved_at=? WHERE resolved_at IS NULL',
               ('later',))
    db.commit()
    # No column or API in S1 exposes trust/confidence for backfilled links.
    columns = {row['name'] for row in db.execute('PRAGMA table_info(fact_evidence_links)')}
    assert not ({'confidence', 'verified', 'support_count'} & columns)
    backfill.close()


def test_scoped_readers_reject_cross_company_access(ledger):
    db = ledger.conn
    put_file(db, SHA, 'good.docx', companies=(COMPANY,))
    fact_a = put_fact(db, COMPANY)
    db.commit()
    registry = SourceRegistry(ledger.path, company_id=COMPANY)
    registry.register_file_source(file_hash=SHA)
    made = registry.add_evidence(file_hash=SHA, locator='l', quote='q')
    registry.link_fact(fact_id=fact_a, evidence_id=made['evidence_id'])
    other = SourceRegistry(ledger.path, company_id=COMPANY_B)
    assert other.get_source(made['source_id']) is None
    with pytest.raises(SourceRejected):
        other.links_for_fact(fact_a)
    with pytest.raises(SourceRejected):
        other.evidence_for_fact(fact_a)
    with pytest.raises(SourceRejected):
        other.distinct_source_ids_for_fact(fact_a)
    registry.close()
    other.close()


# --- group 5: mid-batch failure is fully atomic; batches stay isolated ------

def test_injected_mid_batch_failure_leaves_no_residue(ledger, monkeypatch):
    db = ledger.conn
    put_file(db, SHA, 'good.docx', companies=(COMPANY,))
    f1 = put_fact(db, COMPANY, attribute='甲', source_span=_span())
    f2 = put_fact(db, COMPANY, attribute='乙', source_span=_span(quote='第二份坐标'))
    f3 = put_fact(db, COMPANY, attribute='丙', source_file=None, source_span=None)
    db.commit()

    backfill = SourceBackfill(ledger.path, company_id=COMPANY)
    first = backfill.run(request_sha='batch-a')
    assert first['counts']['linked_facts'] == 2
    assert first['counts']['quarantined'] == 1
    after_a = table_counts(db)

    original = SourceRegistry.link_fact
    calls = {'n': 0}

    def fail_after_first_link(self, **kwargs):
        result = original(self, **kwargs)
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('injected mid-batch failure')
        return result

    monkeypatch.setattr(SourceRegistry, 'link_fact', fail_after_first_link)
    with pytest.raises(RuntimeError, match='injected'):
        backfill.run(request_sha='batch-b')
    assert not db.in_transaction
    assert table_counts(db) == after_a
    receipts = {json.loads(row[0])['request_sha']
                for row in db.execute('SELECT result FROM source_backfill_batches')}
    assert receipts == {'batch-a'}
    rows = db.execute('SELECT batch_id, fact_id FROM source_backfill_quarantine').fetchall()
    assert [(row[0], row[1]) for row in rows] == [(first['batch_id'], f3)]

    monkeypatch.undo()
    recovered = backfill.run(request_sha='batch-b')
    # Batch A already committed f1/f2 credentials; deterministic replay adds none
    # of them again, and f3 is re-quarantined under the new batch only.
    assert recovered['counts']['linked_facts'] == 2
    assert recovered['counts']['links_new'] == 0
    assert recovered['counts']['evidence_new'] == 0
    assert recovered['counts']['quarantined'] == 1
    q_batches = {row[0] for row in db.execute(
        'SELECT DISTINCT batch_id FROM source_backfill_quarantine WHERE fact_id=?', (f3,))}
    assert q_batches == {first['batch_id'], recovered['batch_id']}
    backfill.close()


def test_failed_batch_then_clean_rerun_commits_everything(ledger, monkeypatch):
    db = ledger.conn
    put_file(db, SHA, 'good.docx', companies=(COMPANY,))
    put_fact(db, COMPANY, source_span=_span())
    db.commit()
    backfill = SourceBackfill(ledger.path, company_id=COMPANY)

    original = SourceRegistry.register_file_source

    def fail_once(self, **kwargs):
        fail_once.calls += 1
        if fail_once.calls == 1:
            raise RuntimeError('boom during registration')
        return original(self, **kwargs)
    fail_once.calls = 0
    monkeypatch.setattr(SourceRegistry, 'register_file_source', fail_once)
    with pytest.raises(RuntimeError, match='boom'):
        backfill.run(request_sha='flaky')
    assert table_counts(db)['source_backfill_batches'] == 0
    monkeypatch.undo()
    receipt = backfill.run(request_sha='flaky')
    assert receipt['counts']['linked_facts'] == 1
    assert receipt['counts']['sources_new'] == 1
    stored = backfill.batch(receipt['batch_id'])
    assert stored['finished_at'] is not None
    backfill.close()


# --- group 6: gate default-off; fresh init never creates runtime tables -----

def test_default_off_and_fresh_init_create_no_runtime_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    fresh = tmp_path / 'fresh.db'
    init_db(fresh)
    conn = connect(fresh)
    try:
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not (set(RUNTIME_TABLES) & existing)
        with pytest.raises(RuntimeError, match='facts tree disabled'):
            SourceRegistry(fresh, company_id=COMPANY)
        with pytest.raises(RuntimeError, match='facts tree disabled'):
            SourceBackfill(fresh, company_id=COMPANY)
        # Legacy schema behavior unchanged: fact_leaf_links.source_file stays NOT NULL.
        info = conn.execute('PRAGMA table_info(fact_leaf_links)').fetchall()
        source_file = next(row for row in info if row['name'] == 'source_file')
        assert source_file['notnull'] == 1
    finally:
        conn.close()


def test_gate_open_init_without_construction_still_creates_no_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    fresh = tmp_path / 'fresh2.db'
    init_db(fresh)
    conn = connect(fresh)
    try:
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not (set(RUNTIME_TABLES) & existing)
    finally:
        conn.close()
