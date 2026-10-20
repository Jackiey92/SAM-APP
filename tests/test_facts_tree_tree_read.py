"""F-004-API-001 tree read service: verified projections, no writes, no fallback."""
import json
from pathlib import Path
import sqlite3

import pytest

from app import settings
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.fact_identity import FactIdentityStore
from app.facts_tree.fact_placements import FactPlacementStore
from app.facts_tree.rollup import FactsTreeRollup
from app.facts_tree.template_packs import load_pack
from app.facts_tree.tree_read import TreeReadError, TreeReadService
from app.memory import paths
from app.module_tree import MODULES
from tests.test_facts_tree_leaves import kernel, artifact, publish, COMPANY, SHA

TRUNK_URI = paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-brief.md')
GENERIC = load_pack('generic')


def db_path(kernel):
    return next(row[2] for row in kernel.db.execute('PRAGMA database_list') if row[1] == 'main')


def reader(kernel, company=None):
    return TreeReadService(db_path(kernel), kernel.memory, company_id=company)


def attribute(kernel, fact_id=1, code='L-bizov-08'):
    """Give a fixture fact its verified identity and current unique placement."""
    leaf = GENERIC.leaf_for(code)
    uid = FactIdentityStore(kernel.db).register_initial(
        COMPANY, fact_id, '归属测试', '身份登记')['fact_uid']
    FactPlacementStore(kernel.db, company_id=COMPANY).place_initial(
        COMPANY, fact_id, leaf.trunk, leaf.code, '归属测试', '首次归属')
    return uid


def rolled(kernel):
    publish(kernel)
    attribute(kernel)
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']


def test_disabled_gate_fails_closed(kernel, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(TreeReadError) as error:
        reader(kernel)
    assert (error.value.category, error.value.status) == ('facts_tree_disabled', 503)


def test_unbound_company_state(kernel):
    with reader(kernel) as service:
        for node in ('tree', 'trunk', 'leaf'):
            read = {'tree': service.read_tree, 'trunk': service.read_trunk,
                    'leaf': service.read_leaf}[node]
            result = read('acc-unbound', 'bizov') if node != 'tree' else read('acc-unbound')
            assert result['state'] == 'unbound' and result['binding'] is None
            assert result['company_id'] == 'acc-unbound' and result['node'] == node


def test_empty_bound_tree(kernel):
    with reader(kernel) as service:
        tree = service.read_tree(COMPANY)
        assert tree['state'] == 'empty' and tree['brief'] is None and tree['guide'] is None
        assert len(tree['trunks']) == 10
        assert all(t['state'] == 'not_generated' and t['generated_leaves'] == 0 for t in tree['trunks'])
        bizov = next(t for t in tree['trunks'] if t['code'] == 'bizov')
        assert bizov['leaves'] == 15
        trunk = service.read_trunk(COMPANY, 'bizov')
        assert trunk['state'] == 'not_generated' and trunk['brief'] is None
        assert len(trunk['leaves']) == 15
        assert all(l['state'] == 'not_generated' and l['version'] is None for l in trunk['leaves'])
        leaf = service.read_leaf(COMPANY, 'L-bizov-08')
        assert leaf['state'] == 'not_generated' and leaf['content'] is None
        assert leaf['leaf']['title'] and leaf['uri'] is None and leaf['version'] is None


def test_ready_tree_trunk_leaf(kernel):
    rolled(kernel)
    with reader(kernel) as service:
        tree = service.read_tree(COMPANY)
        assert tree['state'] == 'ready'
        assert tree['brief'] == kernel.memory.read(paths.tree_brief_uri(COMPANY))
        assert tree['guide'] == kernel.memory.read(paths.tree_guide_uri(COMPANY))
        binding = tree['binding']
        assert (binding['template_pack'], binding['pack_version'], binding['binding_revision']) == ('generic', 1, 1)
        bizov = next(t for t in tree['trunks'] if t['code'] == 'bizov')
        assert (bizov['state'], bizov['generated_leaves']) == ('ready', 1)
        trunk = service.read_trunk(COMPANY, 'bizov')
        assert trunk['state'] == 'ready'
        assert trunk['brief'] == kernel.memory.read(TRUNK_URI)
        assert trunk['guide'] == kernel.memory.read(
            paths.trunk_doc_uri(COMPANY, MODULES[1].directory, 'trunk-guide.md'))
        entry = next(l for l in trunk['leaves'] if l['code'] == 'L-bizov-08')
        assert (entry['state'], entry['version']) == ('active', 1)
        leaf = service.read_leaf(COMPANY, 'L-bizov-08')
        assert leaf['state'] == 'ready' and leaf['content'] == artifact()
        assert leaf['version'] == 1 and leaf['leaf_state'] == 'active'
        assert leaf['index']['title'] == '测试叶子' and leaf['index']['sources'] == [SHA]
        assert kernel.memory.read(leaf['uri']) == leaf['content']
        assert service.read_trunk(COMPANY, 'hr')['state'] == 'not_generated'


def test_pending_rollup_is_not_generated(kernel):
    publish(kernel)  # active leaf, L1/L0 projections not yet rolled up
    attribute(kernel)
    with reader(kernel) as service:
        tree = service.read_tree(COMPANY)
        assert tree['state'] == 'not_generated' and tree['brief'] is None
        bizov = next(t for t in tree['trunks'] if t['code'] == 'bizov')
        assert (bizov['state'], bizov['generated_leaves']) == ('not_generated', 1)
        trunk = service.read_trunk(COMPANY, 'bizov')
        assert trunk['state'] == 'not_generated' and trunk['brief'] is None
        assert next(l for l in trunk['leaves'] if l['code'] == 'L-bizov-08')['state'] == 'active'
        assert service.read_leaf(COMPANY, 'L-bizov-08')['state'] == 'ready'


def test_leftover_projection_after_archive_fails_closed(kernel):
    rolled(kernel)
    publish(kernel, artifact(version=2, status='archived'))  # no re-rollup afterwards
    with reader(kernel) as service:
        with pytest.raises(TreeReadError) as error:
            service.read_trunk(COMPANY, 'bizov')
        assert (error.value.category, error.value.status) == ('consistency_failed', 409)
        with pytest.raises(TreeReadError):
            service.read_tree(COMPANY)
        assert service.read_leaf(COMPANY, 'L-bizov-08')['leaf_state'] == 'archived'


def test_unknown_codes_404(kernel):
    with reader(kernel) as service:
        with pytest.raises(TreeReadError) as error:
            service.read_trunk(COMPANY, 'nope')
        assert (error.value.category, error.value.status) == ('unknown_trunk', 404)
        with pytest.raises(TreeReadError) as error:
            service.read_leaf(COMPANY, 'L-bizov-99')
        assert (error.value.category, error.value.status) == ('unknown_leaf', 404)


def test_stale_trunk_projection_fails_closed(kernel):
    rolled(kernel)
    kernel.memory.put(TRUNK_URI, '伪造的无数字结论', metadata={})
    with reader(kernel) as service:
        for read in (lambda: service.read_tree(COMPANY),
                     lambda: service.read_trunk(COMPANY, 'bizov')):
            with pytest.raises(TreeReadError) as error:
                read()
            assert (error.value.category, error.value.status) == ('consistency_failed', 409)
        assert service.read_leaf(COMPANY, 'L-bizov-08')['state'] == 'ready'


def test_tampered_leaf_content_fails_closed(kernel):
    rolled(kernel)
    uri = kernel.db.execute('SELECT uri FROM facts_tree_leaf_index').fetchone()['uri']
    kernel.memory.put(uri, artifact(title='篡改标题'), metadata={})
    with reader(kernel) as service:
        for read in (lambda: service.read_tree(COMPANY),
                     lambda: service.read_trunk(COMPANY, 'bizov'),
                     lambda: service.read_leaf(COMPANY, 'L-bizov-08')):
            with pytest.raises(TreeReadError) as error:
                read()
            assert error.value.status == 409


def test_missing_leaf_projection_fails_closed(kernel):
    rolled(kernel)
    uri = kernel.db.execute('SELECT uri FROM facts_tree_leaf_index').fetchone()['uri']
    kernel.memory.delete(uri)
    with reader(kernel) as service:
        for read in (lambda: service.read_tree(COMPANY),
                     lambda: service.read_trunk(COMPANY, 'bizov'),
                     lambda: service.read_leaf(COMPANY, 'L-bizov-08')):
            with pytest.raises(TreeReadError) as error:
                read()
            assert error.value.status == 409 and 'missing' in str(error.value)


def test_index_without_revision_fails_closed(kernel):
    rolled(kernel)
    kernel.db.execute('DELETE FROM leaf_revisions')
    kernel.db.commit()
    with reader(kernel) as service:
        with pytest.raises(TreeReadError) as error:
            service.read_leaf(COMPANY, 'L-bizov-08')
        assert error.value.status == 409
        with pytest.raises(TreeReadError):
            service.read_tree(COMPANY)


def test_binding_tamper_fails_closed(kernel):
    rolled(kernel)
    kernel.db.execute("UPDATE company_tree_meta SET pack_digest=?", ('0' * 64,))
    kernel.db.commit()
    with reader(kernel) as service:
        with pytest.raises(TreeReadError) as error:
            service.read_tree(COMPANY)
        assert (error.value.category, error.value.status) == ('consistency_failed', 409)


def test_archived_leaf_states(kernel):
    publish(kernel, artifact(status='archived'))
    attribute(kernel)
    with reader(kernel) as service:
        leaf = service.read_leaf(COMPANY, 'L-bizov-08')
        assert leaf['state'] == 'ready' and leaf['leaf_state'] == 'archived'
        trunk = service.read_trunk(COMPANY, 'bizov')
        assert trunk['state'] == 'not_generated'
        assert next(l for l in trunk['leaves'] if l['code'] == 'L-bizov-08')['state'] == 'archived'
        assert service.read_tree(COMPANY)['state'] == 'empty'


def test_reads_never_write(kernel):
    rolled(kernel)
    tables = {r[0] for r in kernel.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    revisions = kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0]
    writes = kernel.memory.writes
    with reader(kernel) as service:
        service.read_tree(COMPANY)
        service.read_trunk(COMPANY, 'bizov')
        service.read_leaf(COMPANY, 'L-bizov-08')
        service.snapshot(COMPANY)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            service.db.execute('CREATE TABLE forbidden (id INTEGER)')
    assert kernel.memory.writes == writes
    assert kernel.db.execute('SELECT count(*) FROM leaf_revisions').fetchone()[0] == revisions
    assert {r[0] for r in kernel.db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == tables


def test_host_scope_and_company_isolation(kernel):
    rolled(kernel)
    binding = CompanyBindingStore(db_path(kernel))
    binding.register('acc-other', '其他公司')
    binding.close()
    with reader(kernel, company=COMPANY) as service:
        with pytest.raises(TreeReadError) as error:
            service.read_tree('acc-other')
        assert (error.value.category, error.value.status) == ('company_scope_violation', 400)
    with reader(kernel) as service:
        other = service.read_tree('acc-other')
        assert other['state'] == 'empty'
        assert all(t['generated_leaves'] == 0 for t in other['trunks'])
        assert service.read_tree(COMPANY)['state'] == 'ready'
        assert service.read_leaf('acc-other', 'L-bizov-08')['state'] == 'not_generated'


def test_snapshot_is_json_and_complete(kernel):
    rolled(kernel)
    with reader(kernel) as service:
        snap = service.snapshot(COMPANY)
        assert snap['company_id'] == COMPANY and snap['state'] == 'ready'
        assert snap['tree'] == service.read_tree(COMPANY)
        assert snap['trunks']['bizov']['state'] == 'ready'
        assert snap['trunks']['hr']['state'] == 'not_generated'
        assert snap['leaves']['L-bizov-08']['content'] == artifact()
        assert snap['leaves']['L-bizov-01']['state'] == 'not_generated'
        assert set(snap['trunks']) == {t['code'] for t in snap['tree']['trunks']}
        assert len(snap['leaves']) == 98
        json.dumps(snap, ensure_ascii=False)
        assert service.snapshot('acc-unbound') == {'company_id': 'acc-unbound', 'state': 'unbound'}


# -- CHG-038: identity/unique-placement verification of indexed facts (DEC-020 A) --

def expect_consistency(service, *reads):
    for read in reads:
        with pytest.raises(TreeReadError) as error:
            read()
        assert (error.value.category, error.value.status) == ('consistency_failed', 409)


def scoped_reads(service):
    return (lambda: service.read_tree(COMPANY),
            lambda: service.read_trunk(COMPANY, 'bizov'),
            lambda: service.read_leaf(COMPANY, 'L-bizov-08'))


def test_indexed_fact_without_identity_fails_closed(kernel):
    publish(kernel)  # the leaf index cites fact 1, which has no identity at all
    writes = kernel.memory.writes
    ledger = Path(db_path(kernel)).read_bytes()
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))
    assert kernel.memory.writes == writes  # no repair, no partial writes
    assert Path(db_path(kernel)).read_bytes() == ledger


def test_foreign_indexed_fact_fails_closed(kernel):
    kernel.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', ('acc-other', '外部主体', '测试指标', '2025-01', '9', '个', SHA, 'bizov', '2026-10-14'))
    kernel.db.commit()
    FactIdentityStore(kernel.db).register_initial('acc-other', 2, '归属测试', '外部身份')
    publish(kernel)  # keep the leaf; then point its index at the foreign fact
    kernel.db.execute("UPDATE leaf_revisions SET fact_ids='[2]'")
    kernel.db.commit()
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))


def test_inactive_identity_fails_closed(kernel):
    rolled(kernel)
    kernel.db.execute('DROP TRIGGER fact_identities_no_update')
    kernel.db.execute("UPDATE fact_identities SET identity_state='retired'")
    kernel.db.commit()
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))


def test_stale_fact_version_fails_closed(kernel):
    rolled(kernel)
    kernel.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (COMPANY, '临时主体', '测试指标', '2025-01', '10', '个', SHA, 'bizov', '2026-10-20'))
    fact = dict(kernel.db.execute('SELECT * FROM facts WHERE id=2').fetchone())
    uid = kernel.db.execute('SELECT fact_uid FROM fact_revisions WHERE fact_row_id=1').fetchone()[0]
    kernel.db.execute('''INSERT INTO fact_revisions(company_id,fact_uid,fact_version,fact_row_id,
        content_sha256,revision_kind,supersedes_row_id,actor,reason,created_at)
        VALUES(?,?,2,?,?,'correction',1,'归属测试','更正','2026-10-20')''',
        (COMPANY, uid, 2, FactIdentityStore._content_digest(fact)))
    kernel.db.execute('UPDATE facts SET superseded_by=2 WHERE id=1')
    kernel.db.commit()
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))  # index cites the old row


def test_unplaced_fact_fails_closed(kernel):
    publish(kernel)
    FactIdentityStore(kernel.db).register_initial(COMPANY, 1, '归属测试', '身份登记')
    with reader(kernel) as service:  # active identity, but no placement at all
        expect_consistency(service, *scoped_reads(service))


@pytest.mark.parametrize('code', ['L-bizov-07', 'L-hr-01'])  # wrong leaf; wrong trunk
def test_wrong_leaf_or_trunk_placement_fails_closed(kernel, code):
    publish(kernel)
    attribute(kernel, code=code)  # placed on another leaf, cited by L-bizov-08
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))


def test_stale_binding_revision_fails_closed(kernel):
    rolled(kernel)  # placement recorded under binding revision 1
    meta = kernel.db.execute('SELECT * FROM company_tree_meta WHERE company_id=?', (COMPANY,)).fetchone()
    kernel.db.execute('INSERT INTO company_template_binding_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (COMPANY, 2, meta['template_pack'], meta['pack_version'], meta['pack_digest'],
                       meta['template_pack'], meta['pack_version'], meta['pack_digest'],
                       '2026-10-20', '归属测试', '合成换绑', None, None, 'rebind'))
    kernel.db.execute('UPDATE company_tree_meta SET binding_revision=2 WHERE company_id=?', (COMPANY,))
    projection = json.loads(kernel.db.execute(
        'SELECT content FROM company_tree_meta_projection WHERE company_id=?', (COMPANY,)).fetchone()[0])
    projection['binding_revision'] = 2
    kernel.db.execute('UPDATE company_tree_meta_projection SET content=? WHERE company_id=?',
                      (json.dumps(projection, ensure_ascii=False, sort_keys=True), COMPANY))
    kernel.db.commit()
    with reader(kernel) as service:  # placement binding revision 1 is no longer current
        expect_consistency(service, *scoped_reads(service))


@pytest.mark.parametrize('damage', ['pointer-missing', 'pointer-trunk', 'history-gap'])
def test_placement_ledger_damage_fails_closed(kernel, damage):
    rolled(kernel)
    if damage == 'pointer-missing':
        kernel.db.execute('DELETE FROM fact_current_placement')
    elif damage == 'pointer-trunk':
        kernel.db.execute("UPDATE fact_current_placement SET trunk_code='hr'")
    else:  # planted history damage: the append-only trigger is lifted synthetically
        kernel.db.execute('DROP TRIGGER fact_placements_no_delete')
        kernel.db.execute('DELETE FROM fact_current_placement')  # pointer FK first
        kernel.db.execute('DELETE FROM fact_placements')
    kernel.db.commit()
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))


@pytest.mark.parametrize('raw', ['not json', '["1"]', '[0]', '{"1": 1}'])
def test_damaged_fact_references_fail_closed(kernel, raw):
    rolled(kernel)
    kernel.db.execute('UPDATE leaf_revisions SET fact_ids=?', (raw,))
    kernel.db.commit()
    with reader(kernel) as service:
        expect_consistency(service, *scoped_reads(service))


def test_cross_leaf_duplicate_reference_fails_closed(kernel):
    rolled(kernel)  # fact 1 is cited by L-bizov-08 and placed there
    publish(kernel, artifact(code='L-bizov-07', data=False), leaf_code='L-bizov-07')
    rollup = FactsTreeRollup(kernel)
    assert rollup.rollup_trunk(company_id=COMPANY, trunk='bizov')['changed']
    assert rollup.rollup_tree(company_id=COMPANY)['changed']
    with reader(kernel) as service:
        with pytest.raises(TreeReadError) as error:  # L-bizov-07 cites fact 1 too
            service.read_tree(COMPANY)
        assert (error.value.category, error.value.status) == ('consistency_failed', 409)
        with pytest.raises(TreeReadError):  # detected via the out-of-scope ID scan
            service.read_leaf(COMPANY, 'L-bizov-08')
        with pytest.raises(TreeReadError):  # ...and the placement points elsewhere
            service.read_leaf(COMPANY, 'L-bizov-07')


# -- CHG-038 fix: one read-only snapshot per request; uniqueness needs revisions --

def test_single_read_snapshot_covers_whole_request(kernel, monkeypatch):
    """Every public read opens one snapshot before its first query, runs all
    consistency reads inside it without nesting, and closes it afterwards."""
    rolled(kernel)
    reads = (lambda s: s.read_tree(COMPANY),
             lambda s: s.read_trunk(COMPANY, 'bizov'),
             lambda s: s.read_leaf(COMPANY, 'L-bizov-08'),
             lambda s: s.snapshot(COMPANY))
    with reader(kernel) as service:
        inside = []
        real = service._verify_placements

        def spied(*args):
            inside.append(service.db.in_transaction)
            return real(*args)

        monkeypatch.setattr(service, '_verify_placements', spied)
        for read in reads:
            statements = []
            service.db.set_trace_callback(statements.append)
            try:
                read(service)
            finally:
                service.db.set_trace_callback(None)
            begins = [s for s in statements if s.lstrip().upper().startswith('BEGIN')]
            assert len(begins) == 1  # one snapshot, never nested
            assert statements[0].lstrip().upper().startswith('BEGIN')  # opened first
            assert not service.db.in_transaction  # closed when the request ended
        assert inside and all(inside)  # every placement verification ran inside it


def test_failed_read_rolls_back_its_snapshot(kernel):
    rolled(kernel)
    kernel.memory.put(TRUNK_URI, '伪造的无数字结论', metadata={})
    with reader(kernel) as service:
        for read in (lambda: service.read_tree(COMPANY),
                     lambda: service.read_trunk(COMPANY, 'bizov')):
            with pytest.raises(TreeReadError):
                read()
            assert not service.db.in_transaction  # rolled back, connection idle
        assert service.read_leaf(COMPANY, 'L-bizov-08')['state'] == 'ready'  # still usable


def test_indexed_leaf_without_revision_breaks_uniqueness(kernel):
    """An indexed leaf with no latest revision makes the cross-leaf uniqueness
    proof impossible; reads fail closed instead of skipping the leaf."""
    rolled(kernel)  # L-bizov-08 cites fact 1 with its verified placement
    publish(kernel, artifact(code='L-bizov-07', data=False, body='无数字正文。[^one]'),
            leaf_code='L-bizov-07', fact_ids=[])
    kernel.db.execute("DELETE FROM leaf_revisions WHERE leaf_code='L-bizov-07'")
    kernel.db.commit()
    with reader(kernel) as service:
        with pytest.raises(TreeReadError) as error:  # uniqueness is unprovable now
            service.read_leaf(COMPANY, 'L-bizov-08')
        assert (error.value.category, error.value.status) == ('consistency_failed', 409)
        assert 'L-bizov-07' in str(error.value)  # raised by the out-of-scope scan
        expect_consistency(service, lambda: service.read_tree(COMPANY),
                           lambda: service.read_trunk(COMPANY, 'bizov'))


def test_empty_reference_leaf_unaffected_by_out_of_scope_damage(kernel, monkeypatch):
    """A scope with no fact citations needs no uniqueness proof: the
    company-wide scan and the placement ledger are never touched, so damage
    on an out-of-scope indexed leaf does not block the read."""
    publish(kernel)  # L-bizov-08 cites fact 1
    publish(kernel, artifact(code='L-bizov-07', data=False, body='无数字正文。[^one]'),
            leaf_code='L-bizov-07', fact_ids=[])  # empty formal references
    kernel.db.execute("DELETE FROM leaf_revisions WHERE leaf_code='L-bizov-08'")
    kernel.db.commit()
    with reader(kernel) as service:
        def forbidden(company):
            raise AssertionError('empty-citation scope must not trigger the all-leaf scan')
        monkeypatch.setattr(service, '_company_fact_refs', forbidden)
        leaf = service.read_leaf(COMPANY, 'L-bizov-07')
        assert leaf['state'] == 'ready' and leaf['code'] == 'L-bizov-07'
        # The citation-carrying tree/trunk reads still fail closed on the damage.
        expect_consistency(service, lambda: service.read_tree(COMPANY),
                           lambda: service.read_trunk(COMPANY, 'bizov'))
