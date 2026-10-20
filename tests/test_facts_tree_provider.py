"""Real sqlite-vec provider, offline embeddings, isolated temporary ledger."""
import pytest

from app import settings
from app.memory import paths
from app.memory.company_facts.l1_brief import publish_pair
from app.memory.provider import LocalVectorMemoryProvider, ProjectionError
from app.module_tree import MODULES, TODO_DIRECTORY
from app.facts_tree.template_packs import load_pack
from app.facts_tree.uris import leaf_uri as pack_leaf_uri, tree_doc_uri, trunk_doc_uri
from tests.memory_setup import migrated_memory


class Embedding:
    dim, embedder, model_version = 2, 'synthetic', 'v1'
    def embed_texts(self, texts): return [[1., 1.] for _ in texts]
    def embed_query(self, text): return [1., 1.]


COMPANY = 'acc-provider'
TRUNK = MODULES[1]
LEAF = paths.leaf_uri(COMPANY, TRUNK.directory, f'L-{TRUNK.key}-01-测试.md')
CONTENT = '# 测试\n\n原件内容[^1]\n\n[^1]: 测试.docx → docx:body/16/table\n'
META = {'company_id': COMPANY, 'owner_skill': 'fact-leaf'}


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    provider = migrated_memory(tmp_path / 'memory.db', embedding=Embedding(), company_id=COMPANY)
    assert isinstance(provider, LocalVectorMemoryProvider)
    yield provider
    provider.close()


def test_leaf_bytes_english_module_null_fact_and_vectors(memory):
    publish_pair(memory, {LEAF: CONTENT}, metadata=META)
    assert memory.read(LEAF) == CONTENT
    row = memory.db.execute('SELECT * FROM m1_documents WHERE uri=?', (LEAF,)).fetchone()
    assert row['module_key'] == TRUNK.key and row['fact_json'] is None
    assert memory.db.execute('SELECT count(*) FROM m1_chunks WHERE uri=?', (LEAF,)).fetchone()[0] > 0
    assert any(r['uri'] == LEAF for r in memory.search('测试', prefix=paths.facts_uri(COMPANY)))


def test_trunk_and_tree_rollups_roundtrip(memory):
    uris = [paths.trunk_doc_uri(COMPANY, TRUNK.directory, name)
            for name in ('trunk-brief.md', 'trunk-guide.md')]
    uris += [paths.tree_brief_uri(COMPANY), paths.tree_guide_uri(COMPANY)]
    publish_pair(memory, {uri: CONTENT for uri in uris}, metadata=META)
    for uri in uris:
        assert memory.read(uri) == CONTENT
        row = memory.db.execute('SELECT module_key,fact_json FROM m1_documents WHERE uri=?', (uri,)).fetchone()
        assert tuple(row) == (TRUNK.key if uri in uris[:2] else '', None)


def test_conflict_roundtrip(memory):
    uri = paths.conflict_uri(COMPANY, f'conflict-{TRUNK.key}-metric-name-job-id.md')
    publish_pair(memory, {uri: CONTENT}, metadata=META)
    assert memory.read(uri) == CONTENT
    assert memory.db.execute('SELECT module_key FROM m1_documents WHERE uri=?', (uri,)).fetchone()[0] == TODO_DIRECTORY


def test_repeat_upsert_has_no_duplicate_documents_or_chunks(memory):
    publish_pair(memory, {LEAF: CONTENT}, metadata=META)
    before = [tuple(r) for r in memory.db.execute('SELECT * FROM m1_chunks ORDER BY chunk_id')]
    publish_pair(memory, {LEAF: CONTENT}, metadata=META)
    assert memory.read(LEAF) == CONTENT
    assert memory.db.execute('SELECT count(*) FROM m1_documents').fetchone()[0] == 1
    assert [tuple(r) for r in memory.db.execute('SELECT * FROM m1_chunks ORDER BY chunk_id')] == before


def test_versioned_pack_paths_resolve_trunk_and_reject_wrong_pack_identity(memory, monkeypatch):
    # This simulates a future release only at the registry boundary. The live
    # registry still keeps generic-v2 unavailable to runtime callers.
    monkeypatch.setattr('app.facts_tree.template_packs.load_released_pack', load_pack)
    pack = load_pack('generic-v2')
    leaf = pack.leaf_for('L-finance-01')
    uri = pack_leaf_uri(COMPANY, pack, leaf)
    address = memory._address(uri)
    assert (address.company_id, address.module_key, address.parts[3:5]) == (
        COMPANY, 'finance', ('generic-v2', '1'))
    trunk_uri = trunk_doc_uri(COMPANY, pack, 'finance', 'trunk-brief.md')
    assert memory._address(trunk_uri).module_key == 'finance'
    tree_uri = tree_doc_uri(COMPANY, pack, 'tree-brief.md')
    assert memory._address(tree_uri).module_key == ''
    prefix = f'sam://facts/co/{COMPANY}/tree/'
    assert memory._search_address(prefix).company_id == COMPANY
    with pytest.raises(ValueError, match='incomplete packed tree document URI'):
        memory._address(prefix)
    with pytest.raises(ValueError, match='incomplete packed tree document URI'):
        memory._address(f'sam://facts/co/{COMPANY}/tree/generic-v2')
    with pytest.raises(ValueError, match='version mismatch'):
        memory._address(uri.replace('/generic-v2/1/', '/generic-v2/2/'))
    with pytest.raises(ValueError, match='does not match'):
        memory._address(uri.replace('L-finance-01-', 'L-legal-01-'))
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(ValueError, match='unavailable while the feature is disabled'):
        memory._address(uri)


def test_unreleased_pack_is_rejected_by_runtime_provider(memory):
    pack = load_pack('generic-v2')
    uri = pack_leaf_uri(COMPANY, pack, pack.leaf_for('L-finance-01'))
    with pytest.raises(ValueError, match='is not released'):
        memory._address(uri)


@pytest.mark.parametrize('direct', [False, True])
def test_delete_and_publish_pair_compensation(memory, direct):
    publish_pair(memory, {LEAF: CONTENT}, metadata=META)
    if direct:
        memory.delete(LEAF)
    else:
        publish_pair(memory, {LEAF: None}, metadata=META)
    with pytest.raises(FileNotFoundError): memory.read(LEAF)
    for table in ('m1_documents', 'm1_chunks'):
        assert memory.db.execute(f'SELECT count(*) FROM {table} WHERE uri=?', (LEAF,)).fetchone()[0] == 0


BAD = [
    LEAF.replace('L-bizov-', 'L-hr-'),
    LEAF.replace('-01-', '-1-'), LEAF.replace('-01-', '-00-'),
    LEAF.replace('leaves/', 'leaves/../'), LEAF.replace('leaves/', 'leaves/extra/'),
    LEAF.replace('测试.md', 'fake.slug.md'), LEAF.replace('测试.md', '.md'),
    LEAF.replace(TRUNK.directory, '99-未知'), LEAF + '/',
    paths.conflict_uri(COMPANY, 'arbitrary.md'),
    paths.conflict_uri(COMPANY, 'conflict-unknown-metric-job.md'),
    paths.conflict_uri(COMPANY, 'conflict-bizov-metric.md'),
    paths.tree_brief_uri(COMPANY) + '/extra',
    paths.tree_meta_uri(COMPANY),
]


@pytest.mark.parametrize('uri', BAD)
def test_invalid_tree_uri_rejected_without_writes(memory, uri):
    with pytest.raises((ValueError, PermissionError)):
        publish_pair(memory, {uri: CONTENT}, metadata=META)
    assert memory.db.execute('SELECT count(*) FROM m1_documents').fetchone()[0] == 0
    assert memory.db.execute('SELECT count(*) FROM m1_chunks').fetchone()[0] == 0


def test_cross_company_metadata_and_uri_rejected(memory):
    for uri, metadata in [(LEAF, {'company_id': 'acc-other'}),
                          (LEAF.replace(COMPANY, 'acc-other'), {'company_id': 'acc-other'})]:
        with pytest.raises(ValueError): publish_pair(memory, {uri: CONTENT}, metadata=metadata)
    assert memory.db.execute('SELECT count(*) FROM m1_documents').fetchone()[0] == 0


def test_failed_batch_rolls_back_bytes_and_vectors_marks_stale(memory, monkeypatch):
    publish_pair(memory, {LEAF: CONTENT}, metadata=META)
    before = [tuple(r) for r in memory.db.execute('SELECT * FROM m1_chunks')]
    real = memory._project
    def fail(row):
        real(row)
        if row['uri'] != LEAF: raise RuntimeError('injected projection failure')
    monkeypatch.setattr(memory, '_project', fail)
    tree = paths.tree_brief_uri(COMPANY)
    with pytest.raises(ProjectionError):
        publish_pair(memory, {LEAF: 'changed', tree: CONTENT}, metadata=META)
    assert memory.read(LEAF) == CONTENT
    with pytest.raises(FileNotFoundError): memory.read(tree)
    assert [tuple(r) for r in memory.db.execute('SELECT * FROM m1_chunks')] == before
    assert memory._state(COMPANY) == 'stale'


def test_closed_tree_rejection_and_legacy_bundle_unchanged(memory, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    for method in (memory.read, memory.delete):
        with pytest.raises(ValueError, match='unknown fact-memory module'): method(LEAF)
    with pytest.raises(ValueError, match='unknown fact-memory module'):
        publish_pair(memory, {LEAF: CONTENT}, metadata=META)
    with pytest.raises(PermissionError, match='bundle only accepts'):
        publish_pair(memory, {paths.tree_brief_uri(COMPANY): CONTENT}, metadata=META)
    for name in ('brief.md', 'guide.md', 'brief.json', 'guide.json'):
        uri = paths.facts_uri(COMPANY, TRUNK.key, name)
        publish_pair(memory, {uri: CONTENT}, metadata=META)
        assert memory.read(uri) == CONTENT
        memory.delete(uri)
        with pytest.raises(FileNotFoundError): memory.read(uri)
