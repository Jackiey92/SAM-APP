import pytest
from app.db import connect, init_db
from app.memory.taxonomy import MODULES, MODULE_KEYS, projection_module
from app.memory.company_facts.l2_fact_verifier import verify_candidate
from app.memory.company_facts.l2_consolidation import ConsolidationService


def test_fact_and_file_taxonomy_are_unified(tmp_path):
    db = tmp_path / 'app.db'
    init_db(db)
    init_db(db)
    assert len(MODULE_KEYS) == 10
    assert {m.key for m in MODULES if m.hidden} == {'prod', 'purch', 'sales', 'fin', 'fund'}
    assert all('_' not in m.directory for m in MODULES)
    for old in ('finance', 'other', '', None, ' fin'):
        assert projection_module(old) == '待归类'
    with connect(db) as conn:
        assert MODULE_KEYS == {r['code'] for r in conn.execute('SELECT code FROM modules')}
        assert not any(r['from'] == 'module_key' for r in conn.execute('PRAGMA foreign_key_list(facts)'))
        col = next(r for r in conn.execute('PRAGMA table_info(facts)') if r['name'] == 'module_key')
        assert col['notnull'] == 0 and col['dflt_value'] is None


def test_employee_module_survives_consolidation(tmp_path):
    db = tmp_path / 'app.db'
    init_db(db)
    from app.storage import ArchiveFileStore
    stored = ArchiveFileStore(tmp_path / "objects", db).put_bytes(b"synthetic gudou", original_name="gudou.txt")
    fact = verify_candidate({'metric': '收入', 'value': '3', 'quote': '收入3', 'module_key': 'fin'},
                            [{'content': '谷斗收入3', 'file_hash': stored.file_hash}], company_id='acc-gudou')
    ConsolidationService(db).consolidate_candidates([fact])
    assert ConsolidationService(db).list_verified_facts(company_id='acc-gudou')[0]['module_key'] == 'fin'


@pytest.mark.parametrize("module", ["fin", "legal"])
def test_projection_reads_only_current_facts(tmp_path, module):
    from tests.memory_setup import migrated_memory
    from app.memory import paths
    from app.storage import ArchiveFileStore
    from app.memory.company_facts.l2_fact_extractor import ExtractedFact
    class Embedding:
        dim, embedder, model_version = 2, 'synthetic', 'v1'
        def embed_texts(self, texts): return [[1., 1.] for _ in texts]
        def embed_query(self, query): return [1., 1.]
    db = tmp_path / 'app.db'
    provider = migrated_memory(db, embedding=Embedding())
    try:
        source = ArchiveFileStore(tmp_path / 'objects', db).put_bytes(b'gudou', original_name='gudou.txt')
        service = ConsolidationService(db)
        candidate = ExtractedFact('acc-gudou', '收入', '2024', '3', 'number', '元',
                                  source.file_hash, 1, 'row/1', 1., False, module_key=module)
        result = service.consolidate_candidates([candidate])
        provider.rebuild_projection('acc-gudou')
        assert provider.search('收入', prefix=paths.facts_uri('acc-gudou'))[0]['module_key'] == module
        assert provider.db.execute('SELECT COUNT(*) FROM m1_documents').fetchone()[0] == 0
        with connect(db) as conn:
            conn.execute('UPDATE facts SET module_key=? WHERE id=?', ('finance', result.verified_fact_ids[0]))
            conn.commit()
        # Exact unsupported employee key, not finance -> fin.
        assert provider.search('收入', prefix=paths.facts_uri('acc-gudou'))[0]['module_key'] == '待归类'
        with connect(db) as conn:
            conn.execute("UPDATE facts SET status='pending' WHERE id=?", result.verified_fact_ids)
            conn.commit()
        assert provider.search('收入', prefix=paths.facts_uri('acc-gudou')) == []
    finally:
        provider.close()


def test_legal_taxonomy_definition():
    assert [(m.key, m.directory, m.order, m.hidden) for m in MODULES][-1] == ("legal", "10-法务合规", 10, False)
    assert [m.order for m in MODULES] == list(range(1, 11))
    assert projection_module("legal") == "legal"
