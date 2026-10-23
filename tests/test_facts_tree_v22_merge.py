"""Protocol V4 V2-2: same-leaf body merging (compare_submit ``merges``).

Real isolated SQLite ledgers plus offline scripted employees; no models, no
credentials, no network, no real company data. Every merge must carry each old
evidence ref exactly once (host-copied, only the ref label renamed), keep every
old fact id, re-verify carried facts/coordinates/persisted quotes against the
fact ledger, and land one append-only old/new rewrite-audit row inside the same
transaction. V2-1 digests, V2/V3 submissions, the closed gate and the missing
publish budget keep their frozen behavior. The CHG-036 section verifies that
service-level merges also re-check every carried fact's active identity,
current revision and exact current leaf placement against the versioned
attribution ledger, failing closed with a full rollback on any drift. The
generic-v2 section drives the same two-job same-leaf merge on the unreleased
draft pack's finance trunk through test-scoped binding/projection seams, then
re-verifies the restored formal gate still hides and refuses the draft pack.
"""
from copy import deepcopy
import json
import runpy
import sqlite3

import pytest

from app import settings
from app.document_read.dossier import Dossier
from app.facts_tree import template_packs
from app.facts_tree.company_binding import CompanyBindingStore
from app.facts_tree.fact_identity import FactIdentityStore
from app.facts_tree.fact_placements import FactPlacementStore
from app.facts_tree.leaf_store import LeafPublishService, LeafRejected
from app.facts_tree.md_publish import MDLeafPublisher, digest
from app.facts_tree.md_reader import MDWindows
from app.facts_tree.notes_publish import NotesPublishService, PublishRejected
from app.facts_tree.quote_sources import DossierQuoteResolver
from app.facts_tree.template_packs import available_packs, load_pack
from app.memory.provider import ProjectionError
from tests.memory_setup import migrated_memory
from tests.test_facts_tree_transactions import real_store
from tests.test_facts_tree_leaves import COMPANY, SHA, artifact
from tests.test_facts_tree_provider import Embedding
from tests.test_facts_tree_reading_notes import dossier, reader, LOC0, LOC1, JOB, REVIEW_LIMITS
from tests.test_facts_tree_notes_publish import PublishModel, PUBLISH_LIMITS, registry_counts

MERGE_LOC = 'docx:body/3/paragraph'
JOB2 = 'v4-job-2'
LEAF = 'L-bizov-07'


# ---------------------------------------------------------------------------
# Publisher-level helpers (direct MDLeafPublisher, offline dossier grants).
# ---------------------------------------------------------------------------

@pytest.fixture
def pub(real_store):
    services = []

    def make(d=None, *, protocol=4):
        d = d or dossier()
        service = LeafPublishService(real_store.memory.database, real_store.memory,
            company_id=COMPANY, quote_resolver=DossierQuoteResolver({SHA: d.document}))
        services.append(service)
        return MDLeafPublisher(service, reading_protocol=protocol)

    yield real_store, make
    for service in services:
        service.close()


def grants_for(d):
    windows = MDWindows(d)
    grants = {}
    for window in windows.windows:
        grant = windows.grant(window, 'sample.docx')
        grant['company_id'] = COMPANY
        grants[grant['grant_id']] = grant
    return grants


def rich_dossier():
    doc = dict(markdown='技术描述可回查，有9个。\n| 项目 | 值 |\n| --- | --- |\n| 参数 | 9 |\n分3批交付，每批3个。',
               document_map=dict(elements=[dict(source_loc=LOC0, start_line=1, end_line=1),
                   dict(source_loc=LOC1, start_line=2, end_line=4, kind='table'),
                   dict(source_loc=MERGE_LOC, start_line=5, end_line=5)]), assets=[])
    d = Dossier(doc, file_hash=SHA, objects_root='.')
    d.quality_fingerprint = d.fingerprint
    return d


def mint_fact(store, d, *, value, unit, loc, quote, lines, period='unknown',
              note_id='seed-note', v4=True):
    witness = dict(source_loc=loc, quote=quote, mapped_fingerprint=d.fingerprint,
                   field_evidence=[])
    if lines is not None:
        witness['ranges'] = [list(lines)]
    if v4:
        span = dict(witness, reading_protocol=4,
                    sources=[dict(witness, note_id=note_id, grant_id='0')])
    else:
        span = witness
    cursor = store.db.execute(
        '''INSERT INTO facts(company_id,entity,attribute,period,value,value_type,unit,
            source_file,source_span,valid_from,confidence,status,confirm_mode,created_at,module_key)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (COMPANY, '临时主体', '测试指标', period, value, 'number', unit, SHA,
         json.dumps(span, ensure_ascii=False), 'now', 1.0, 'verified', 'auto', 'now', 'bizov'))
    store.db.commit()
    return int(cursor.lastrowid)


def evidence(grant, fact_ids, *, ref='a', note_id='seed-note', start=None, end=None):
    if start is None:
        start = end = grant['ranges'][-1][1]
    entry = dict(ref=ref, grant_id=grant['grant_id'], file_hash=SHA,
                 original_name='sample.docx', source_loc=grant['source_loc'],
                 start_line=start, end_line=end, fact_ids=list(fact_ids))
    if note_id is not None:
        entry['note_id'] = note_id
    return entry


def seeded_leaf(store, make, d=None, *, protocol=4, code=LEAF):
    """Two V4 blocks old-a/old-b on one leaf; returns (publisher, grants, fact ids)."""
    d = d or dossier()
    publisher = make(d, protocol=protocol)
    grants = grants_for(d)
    f1 = mint_fact(store, d, value='9', unit='个', loc=LOC0, quote='有9个',
                   lines=[1, 1], note_id='seed-note-a')
    f2 = mint_fact(store, d, value='9', unit='', loc=LOC1, quote='| 参数 | 9 |',
                   lines=[4, 4], note_id='seed-note-b')
    blocks = [
        dict(block_id='old-a', text='技术描述可回查，有9个[^a]。',
             evidence=[evidence(grants['0'], [f1], ref='a', note_id='seed-note-a', start=1, end=1)]),
        dict(block_id='old-b', text='参数值为9[^b]。',
             evidence=[evidence(grants['1'], [f2], ref='b', note_id='seed-note-b', start=4, end=4)]),
    ]
    publisher.submit(company=COMPANY, code=code, operation_id='seed-op',
                     base_version=0, base_sha='', blocks=blocks, conflicts=[],
                     grants=grants, job_id='job-md', note='基线发布')
    return publisher, grants, (f1, f2)


CARRY_A = dict(from_block_id='old-a', from_ref='a', to_ref='x')
CARRY_B = dict(from_block_id='old-b', from_ref='b', to_ref='y')
MERGE_REASON = '员工判断两旧块同主题，合并呈现且事实证据全量保留'


def standard_merge(overrides=None):
    merge = dict(block_id='merged-1',
                 text='合并陈述：技术描述可回查，有9个[^x]；参数值为9[^y]。',
                 covers=['old-a', 'old-b'], carry=[dict(CARRY_A), dict(CARRY_B)],
                 reason=MERGE_REASON)
    if overrides:
        merge.update(overrides)
    return merge


def merge_submit(publisher, grants, merges, *, op='merge-op', blocks=None, conflicts=None,
                 base=None, code=LEAF):
    base = base or publisher.current(COMPANY, code)
    return publisher.submit(company=COMPANY, code=code, operation_id=op,
        base_version=base['version'], base_sha=base['content_sha'],
        blocks=blocks if blocks is not None else [], conflicts=conflicts or [],
        grants=grants, job_id='job-md', note='员工合并发布', merges=merges)


def assert_leaf_untouched(store, publisher, *, facts, blocks=('old-a', 'old-b'),
                          operations=1, code=LEAF):
    current = publisher.current(COMPANY, code)
    assert current['version'] == 1
    assert [b['block_id'] for b in current['blocks']] == list(blocks)
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == operations
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == facts


# ---------------------------------------------------------------------------
# Happy path: carry, audit, deterministic order, replay, append compat.
# ---------------------------------------------------------------------------

def test_happy_merge_carries_evidence_rewrites_audit_and_replays(pub):
    store, make = pub
    publisher, grants, (f1, f2) = seeded_leaf(store, make)
    before = publisher.current(COMPANY, LEAF)
    receipt = merge_submit(publisher, grants, [standard_merge()], base=before)
    assert receipt['changed'] and receipt['version'] == 2
    current = publisher.current(COMPANY, LEAF)
    assert [b['block_id'] for b in current['blocks']] == ['merged-1']
    merged = current['blocks'][0]
    old_refs = {e['ref']: e for b in before['blocks'] for e in b['evidence']}
    by_ref = {e['ref']: e for e in merged['evidence']}
    # Carried evidence is host-copied verbatim; only the ref label changed.
    assert by_ref['x'] == dict(old_refs['a'], ref='x')
    assert by_ref['y'] == dict(old_refs['b'], ref='y')
    assert by_ref['x']['fact_ids'] == [f1] and by_ref['y']['fact_ids'] == [f2]
    # One append-only rewrite-audit row with the full old/new snapshots.
    rows = store.db.execute('SELECT * FROM md_leaf_rewrites').fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row['company_id'], row['operation_id'], row['leaf_code']) == (COMPANY, 'merge-op', LEAF)
    assert row['base_version'] == 1 and row['base_sha'] == before['content_sha']
    assert row['old_markdown'] == before['markdown'] and row['new_markdown'] == current['markdown']
    assert json.loads(row['old_blocks']) == before['blocks']
    assert json.loads(row['new_blocks']) == current['blocks']
    assert json.loads(row['coverage']) == [dict(block_id='merged-1', covers=['old-a', 'old-b'],
        carry=[dict(CARRY_A), dict(CARRY_B)], reason=MERGE_REASON)]
    # Identical replay returns the stored receipt without new audit writes.
    again = merge_submit(publisher, grants, [standard_merge()], base=before)
    assert again == receipt
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2
    # Same operation id with a tampered request is a hard rejection.
    with pytest.raises(LeafRejected, match='operation replay differs'):
        merge_submit(publisher, grants, [standard_merge({'reason': '篡改合并理由'})], base=before)
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1


def test_merge_output_order_walks_base_blocks_and_appends(pub):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)
    d = dossier()
    f3 = mint_fact(store, d, value='9', unit='个', loc=LOC0, quote='有9个',
                   lines=[1, 1], note_id='seed-note-c')
    current = publisher.current(COMPANY, LEAF)
    third = dict(block_id='old-c', text='第三段陈述：有9个[^c]。',
        evidence=[evidence(grants['0'], [f3], ref='c', note_id='seed-note-c', start=1, end=1)])
    publisher.submit(company=COMPANY, code=LEAF, operation_id='seed-op-2',
        base_version=current['version'], base_sha=current['content_sha'],
        blocks=[deepcopy(b) for b in current['blocks']] + [third],
        conflicts=[], grants=grants, job_id='job-md', note='追加第三块')
    current = publisher.current(COMPANY, LEAF)
    old_c = deepcopy(current['blocks'][2])
    tail = dict(block_id='new-tail', text='尾巴陈述[^t]。',
        evidence=[evidence(grants['0'], [], ref='t', note_id='seed-note-c', start=1, end=1)])
    # merges order is deliberately reversed against the base order.
    m1 = dict(block_id='merge-b', covers=['old-b'],
              carry=[dict(from_block_id='old-b', from_ref='b', to_ref='mb')],
              text='合并参数值为9[^mb]。', reason='员工合并参数块')
    m2 = dict(block_id='merge-a', covers=['old-a'],
              carry=[dict(from_block_id='old-a', from_ref='a', to_ref='ma')],
              text='合并描述有9个[^ma]。', reason='员工合并描述块')
    receipt = merge_submit(publisher, grants, [m1, m2], blocks=[old_c, tail])
    assert receipt['changed'] and receipt['version'] == 3
    current = publisher.current(COMPANY, LEAF)
    # First covered position hosts the merge output; uncovered stay; additions append.
    assert [b['block_id'] for b in current['blocks']] == ['merge-a', 'merge-b', 'old-c', 'new-tail']
    row = store.db.execute('SELECT coverage FROM md_leaf_rewrites').fetchone()
    assert [c['block_id'] for c in json.loads(row['coverage'])] == ['merge-b', 'merge-a']


def test_plain_append_still_works_and_writes_no_rewrite_audit(pub):
    store, make = pub
    d = rich_dossier()
    publisher, grants, _ = seeded_leaf(store, make, d)
    f3 = mint_fact(store, d, value='3', unit='批', loc=MERGE_LOC,
                   quote='分3批交付，每批3个', lines=[5, 5], note_id='seed-note-c')
    current = publisher.current(COMPANY, LEAF)
    blocks = [deepcopy(b) for b in current['blocks']] + [
        dict(block_id='old-c', text='分3批交付[^c]。',
             evidence=[evidence(grants['2'], [f3], ref='c', note_id='seed-note-c', start=5, end=5)])]
    receipt = merge_submit(publisher, grants, None, blocks=blocks)
    assert receipt['changed'] and receipt['version'] == 2
    # Pure append: no rewrite-audit row, the V2-1 receipt shape is unchanged.
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 2


def test_empty_merges_keep_v21_request_digest(pub):
    store, make = pub
    d = dossier()
    publisher = make(d)
    grants = grants_for(d)
    block = dict(block_id='block-a', text='技术描述可回查，有9个[^a]。',
                 evidence=[evidence(grants['0'], [], ref='a', start=1, end=1)])
    publisher.submit(company=COMPANY, code=LEAF, operation_id='op-empty-merges',
        base_version=0, base_sha='', blocks=[block], conflicts=[], grants=grants,
        job_id='job-md', note='记录', merges=[])
    row = store.db.execute(
        "SELECT request_sha FROM md_leaf_operations WHERE operation_id='op-empty-merges'").fetchone()
    expected = digest(dict(code=LEAF, base_version=0, base_sha='', blocks=[block],
                           conflicts=[], job_id='job-md', note='记录'))
    assert row['request_sha'] == expected


# ---------------------------------------------------------------------------
# Fail-closed validation: forged/missing carry, coverage, labels, numbers.
# ---------------------------------------------------------------------------

REJECT_CASES = [
    ('covers-ghost', {'covers': ['old-a', 'ghost']}, 'unknown or cross-leaf block'),
    ('covers-empty', {'covers': []}, 'distinct covered block ids'),
    ('covers-dup', {'covers': ['old-a', 'old-a']}, 'distinct covered block ids'),
    ('carry-missing', {'carry': [dict(CARRY_A)]}, 'carried exactly once'),
    ('carry-ghost-ref',
     {'carry': [dict(CARRY_A), dict(from_block_id='old-b', from_ref='ghost', to_ref='y')]},
     'unknown old evidence'),
    ('carry-outside',
     {'carry': [dict(CARRY_A), dict(from_block_id='ghost', from_ref='b', to_ref='y')]},
     'outside the covered blocks'),
    ('carry-extra-field',
     {'carry': [dict(CARRY_A, file_hash=SHA), dict(CARRY_B)]}, 'host-copied'),
    ('carry-dup', {'carry': [dict(CARRY_A), dict(CARRY_A), dict(CARRY_B)]}, 'more than once'),
    ('to-ref-dup',
     {'carry': [dict(CARRY_A), dict(from_block_id='old-b', from_ref='b', to_ref='x')]},
     'duplicate carried'),
    ('reason-blank', {'reason': '  '}, 'nonempty employee reason'),
    ('block-id-reuse', {'block_id': 'old-b'}, 'fresh block id'),
    ('extra-key', {'rogue': 1}, 'block_id/text/covers/carry/reason'),
    ('number-fabricated',
     {'text': '合并陈述：有10个[^x]；参数值为9[^y]。'}, 'number without fact or quote source'),
    ('unit-wrong',
     {'text': '合并陈述：有9元[^x]；参数值为9[^y]。'}, 'number without fact or quote source'),
]


@pytest.mark.parametrize('name,overrides,snippet', REJECT_CASES,
                         ids=[case[0] for case in REJECT_CASES])
def test_illegal_merge_rejected_with_zero_side_effects(pub, name, overrides, snippet):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)
    with pytest.raises(LeafRejected, match=snippet):
        merge_submit(publisher, grants, [standard_merge(overrides)])
    assert_leaf_untouched(store, publisher, facts=3)


NEW_EVIDENCE_BAD_VALUES = [None, {}, '', 0, False]


@pytest.mark.parametrize('value', NEW_EVIDENCE_BAD_VALUES,
                         ids=['null', 'object', 'string', 'zero', 'false'])
def test_merge_new_evidence_wrong_type_rejected_publisher(pub, value):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)
    with pytest.raises(LeafRejected, match='new_evidence must be a list'):
        merge_submit(publisher, grants, [standard_merge({'new_evidence': value})])
    assert_leaf_untouched(store, publisher, facts=3)


def test_merge_new_evidence_explicit_empty_array_allowed_publisher(pub):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)
    # Absent is the default everywhere else; an explicit empty array is legal too.
    receipt = merge_submit(publisher, grants, [standard_merge({'new_evidence': []})])
    assert receipt['changed'] and receipt['version'] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1


def test_covered_block_resubmitted_or_double_covered_rejected(pub):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)
    current = publisher.current(COMPANY, LEAF)
    with pytest.raises(LeafRejected, match='must not be resubmitted verbatim'):
        merge_submit(publisher, grants, [standard_merge()],
                     blocks=[deepcopy(current['blocks'][0])])
    with pytest.raises(LeafRejected, match='more than one merge'):
        merge_submit(publisher, grants,
                     [standard_merge(), standard_merge({'block_id': 'merged-2'})])
    assert_leaf_untouched(store, publisher, facts=3)


def test_conflict_old_block_cannot_be_merged_but_todo_still_works(pub):
    store, make = pub
    publisher, grants, (f1, f2) = seeded_leaf(store, make)
    current = publisher.current(COMPANY, LEAF)
    conflict = dict(old_block_id='old-a', reason='员工判断旧块与新证据冲突，留待办不裁决',
        new_block=dict(block_id='fix-a', text='更准陈述：有9个[^c]。',
            evidence=[evidence(grants['0'], [f1], ref='c', note_id='seed-note-a', start=1, end=1)]))
    with pytest.raises(LeafRejected, match='conflict old block cannot be merged away'):
        merge_submit(publisher, grants, [standard_merge()], conflicts=[conflict])
    assert_leaf_untouched(store, publisher, facts=3)
    # The conflict-only path is untouched: both old sides stay, one todo lands.
    receipt = merge_submit(publisher, grants, None,
                           blocks=[deepcopy(b) for b in current['blocks']],
                           conflicts=[conflict])
    assert receipt['todos'] == 1 and not receipt['changed']
    assert publisher.current(COMPANY, LEAF)['version'] == 1
    payload = json.loads(store.db.execute('SELECT payload FROM md_leaf_todos').fetchone()[0])
    assert payload['old']['block_id'] == 'old-a' and payload['new']['block_id'] == 'fix-a'
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Carried numeric support: persisted ledger quotes only, never old leaf text.
# ---------------------------------------------------------------------------

def test_carried_quote_supports_only_persisted_tokens_not_old_body(pub):
    store, make = pub
    d = rich_dossier()
    publisher = make(d)
    grants = grants_for(d)
    f = mint_fact(store, d, value='3', unit='批', loc=MERGE_LOC,
                  quote='分3批交付', lines=[5, 5])
    block = dict(block_id='old-a', text='分3批交付，每批3个[^a]。',
                 evidence=[evidence(grants['2'], [f], ref='a', start=5, end=5)])
    publisher.submit(company=COMPANY, code=LEAF, operation_id='seed-op',
                     base_version=0, base_sha='', blocks=[block], conflicts=[],
                     grants=grants, job_id='job-md', note='基线发布')
    carry = [dict(from_block_id='old-a', from_ref='a', to_ref='x')]
    # '每批3个' lived in the old body and the read grant, never in the
    # persisted ledger quote: the merged text cannot reuse it.
    bad = standard_merge({'text': '合并陈述：每批3个[^x]。', 'covers': ['old-a'], 'carry': carry})
    with pytest.raises(LeafRejected, match='number without fact or quote source'):
        merge_submit(publisher, grants, [bad], op='merge-bad')
    assert_leaf_untouched(store, publisher, facts=2, blocks=('old-a',))
    # '分3批' is backed by the fact fields AND the persisted quote: it carries.
    good = standard_merge({'text': '合并陈述：分3批[^x]。', 'covers': ['old-a'], 'carry': carry})
    receipt = merge_submit(publisher, grants, [good], op='merge-good')
    assert receipt['changed'] and receipt['version'] == 2


def test_factless_old_evidence_carries_but_never_supports_numbers(pub):
    store, make = pub
    d = dossier()
    publisher = make(d)
    grants = grants_for(d)
    block = dict(block_id='old-a', text='技术描述可回查，有9个[^a]。',
                 evidence=[evidence(grants['0'], [], ref='a', start=1, end=1)])
    publisher.submit(company=COMPANY, code=LEAF, operation_id='seed-op',
                     base_version=0, base_sha='', blocks=[block], conflicts=[],
                     grants=grants, job_id='job-md', note='基线发布')
    carry = [dict(from_block_id='old-a', from_ref='a', to_ref='x')]
    bad = standard_merge({'text': '合并陈述：有9个[^x]。', 'covers': ['old-a'], 'carry': carry})
    with pytest.raises(LeafRejected, match='number without fact or quote source'):
        merge_submit(publisher, grants, [bad], op='merge-bad')
    assert_leaf_untouched(store, publisher, facts=1, blocks=('old-a',))
    good = standard_merge({'text': '合并陈述：技术描述可回查[^x]。', 'covers': ['old-a'],
                           'carry': carry})
    receipt = merge_submit(publisher, grants, [good], op='merge-good')
    assert receipt['changed']
    merged = publisher.current(COMPANY, LEAF)['blocks'][0]
    assert merged['evidence'][0]['fact_ids'] == [] and merged['evidence'][0]['ref'] == 'x'


# ---------------------------------------------------------------------------
# V3 carry and tampered-ledger fail-closed revalidation.
# ---------------------------------------------------------------------------

def test_v3_evidence_carried_through_consistent_primary_span(pub):
    store, make = pub
    d = dossier()
    publisher3 = make(d, protocol=3)
    grants = grants_for(d)
    f = mint_fact(store, d, value='9', unit='个', loc=LOC0, quote='有9个',
                  lines=[1, 1], v4=False)
    block = dict(block_id='old-a', text='技术描述可回查，有9个[^a]。',
                 evidence=[evidence(grants['0'], [f], ref='a', note_id=None, start=1, end=1)])
    publisher3.submit(company=COMPANY, code=LEAF, operation_id='seed-op',
                      base_version=0, base_sha='', blocks=[block], conflicts=[],
                      grants=grants, job_id='job-md', note='基线发布')
    publisher4 = make(d, protocol=4)
    merge = standard_merge({'text': '合并陈述：有9个[^x]。', 'covers': ['old-a'],
                            'carry': [dict(from_block_id='old-a', from_ref='a', to_ref='x')]})
    receipt = merge_submit(publisher4, grants, [merge])
    assert receipt['changed'] and receipt['version'] == 2
    merged = publisher4.current(COMPANY, LEAF)['blocks'][0]
    assert merged['evidence'][0]['fact_ids'] == [f]
    assert 'note_id' not in merged['evidence'][0]
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1


def test_unpositioned_legacy_span_carry_rejected(pub):
    store, make = pub
    d = dossier()
    publisher2 = make(d, protocol=2)
    grants = grants_for(d)
    f = mint_fact(store, d, value='9', unit='个', loc=LOC0, quote='有9个',
                  lines=None, v4=False)
    block = dict(block_id='old-a', text='技术描述可回查，有9个[^a]。',
                 evidence=[evidence(grants['0'], [f], ref='a', note_id=None, start=1, end=1)])
    publisher2.submit(company=COMPANY, code=LEAF, operation_id='seed-op',
                      base_version=0, base_sha='', blocks=[block], conflicts=[],
                      grants=grants, job_id='job-md', note='基线发布')
    publisher4 = make(d, protocol=4)
    merge = standard_merge({'text': '合并陈述：有9个[^x]。', 'covers': ['old-a'],
                            'carry': [dict(from_block_id='old-a', from_ref='a', to_ref='x')]})
    with pytest.raises(LeafRejected, match='carried line range differs from the fact ledger'):
        merge_submit(publisher4, grants, [merge])
    assert_leaf_untouched(store, publisher4, facts=2, blocks=('old-a',))


TAMPER_CASES = [
    ('company', 'invalid or cross-trunk fact reference'),
    ('trunk', 'invalid or cross-trunk fact reference'),
    ('source-file', 'invalid fact leaf assignment'),
    ('coordinate', 'carried coordinate differs from the fact ledger'),
    ('note', 'not a member witness of the fact'),
]


@pytest.mark.parametrize('attack,snippet', TAMPER_CASES, ids=[case[0] for case in TAMPER_CASES])
def test_carried_fact_revalidation_fail_closed(pub, attack, snippet):
    store, make = pub
    publisher, grants, (f1, f2) = seeded_leaf(store, make)
    if attack == 'company':
        store.db.execute("UPDATE facts SET company_id='acc-other' WHERE id=?", (f1,))
    elif attack == 'trunk':
        store.db.execute("UPDATE facts SET module_key='fin' WHERE id=?", (f1,))
    elif attack == 'source-file':
        store.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) '
                         'VALUES(?,?,?,?,?)', ('b' * 64, 'other.docx', 1, '/unused', 'now'))
        store.db.execute('UPDATE facts SET source_file=? WHERE id=?', ('b' * 64, f1))
    else:
        span = json.loads(store.db.execute(
            'SELECT source_span FROM facts WHERE id=?', (f1,)).fetchone()[0])
        if attack == 'coordinate':
            span['source_loc'] = 'docx:body/99/paragraph'
            span['sources'][0]['source_loc'] = 'docx:body/99/paragraph'
        else:
            span['sources'][0]['note_id'] = 'other-note'
        store.db.execute('UPDATE facts SET source_span=? WHERE id=?',
                         (json.dumps(span, ensure_ascii=False), f1))
    store.db.commit()
    with pytest.raises(LeafRejected, match=snippet):
        merge_submit(publisher, grants, [standard_merge()])
    assert_leaf_untouched(store, publisher, facts=3)


# ---------------------------------------------------------------------------
# Boundaries: V2/V3 protocols, legacy leaves, closed gate, append-only audit.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('protocol', [2, 3])
def test_protocol_2_3_reject_merges_but_plain_submit_works(pub, protocol):
    store, make = pub
    d = dossier()
    publisher = make(d, protocol=protocol)
    grants = grants_for(d)
    merge = dict(block_id='merged-1', covers=['old-a'], carry=[], text='x', reason='r')
    with pytest.raises(LeafRejected, match='same-leaf body merge requires reading protocol 4'):
        publisher.submit(company=COMPANY, code=LEAF, operation_id='op-m',
            base_version=0, base_sha='', blocks=[], conflicts=[], grants=grants,
            job_id='job-md', note='n', merges=[merge])
    block = dict(block_id='block-a', text='技术描述可回查，有9个[^a]。',
                 evidence=[evidence(grants['0'], [], ref='a', note_id=None, start=1, end=1)])
    receipt = publisher.submit(company=COMPANY, code=LEAF, operation_id='op-plain',
        base_version=0, base_sha='', blocks=[block], conflicts=[], grants=grants,
        job_id='job-md', note='n')
    assert receipt['changed'] and receipt['version'] == 1
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0


def test_legacy_leaf_cannot_be_merged(pub):
    store, make = pub
    d = dossier()
    text = artifact(code=LEAF).replace('docx:body/77/paragraph', LOC0)
    store.publish_leaf(company_id=COMPANY, leaf_code=LEAF, markdown=text,
                       source_files=[SHA], fact_ids=[1], job_id='job')
    publisher = make(d)
    grants = grants_for(d)
    current = publisher.current(COMPANY, LEAF)
    assert current['legacy']
    merge = dict(block_id='merged-1', covers=['legacy'], carry=[], text='合并陈述[^x]。',
                 reason='员工试图合并旧格式叶')
    with pytest.raises(LeafRejected, match='legacy leaf cannot be merged'):
        merge_submit(publisher, grants, [merge], base=current)
    assert publisher.current(COMPANY, LEAF)['version'] == 1
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0


def test_gate_closed_rejects_publisher_construction(real_store, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', False)
    with pytest.raises(RuntimeError, match='facts tree disabled'):
        MDLeafPublisher(real_store, reading_protocol=4)


def test_rewrite_audit_is_append_only(pub):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)
    merge_submit(publisher, grants, [standard_merge()])
    with pytest.raises(Exception, match='append-only rewrite audit'):
        store.db.execute("UPDATE md_leaf_rewrites SET coverage='x'")
    store.db.rollback()
    with pytest.raises(Exception, match='append-only rewrite audit'):
        store.db.execute('DELETE FROM md_leaf_rewrites')
    store.db.rollback()
    row = store.db.execute('SELECT * FROM md_leaf_rewrites').fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute('INSERT INTO md_leaf_rewrites VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         tuple(row))
    store.db.rollback()
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1


def test_projection_failure_rolls_back_merge(pub, monkeypatch):
    store, make = pub
    publisher, grants, _ = seeded_leaf(store, make)

    def fail(texts):
        raise RuntimeError('injected embedding failure')

    monkeypatch.setattr(store.memory.embedding, 'embed_texts', fail)
    prior_projection = store.memory.projection_status(COMPANY)
    with pytest.raises(ProjectionError, match='caller must roll back'):
        merge_submit(publisher, grants, [standard_merge()])
    assert_leaf_untouched(store, publisher, facts=3)
    assert not publisher.store.db.in_transaction
    assert store.memory.projection_status(COMPANY) == prior_projection
    uri = store.db.execute('SELECT uri FROM facts_tree_leaf_index WHERE company_id=? AND leaf_code=?',
                           (COMPANY, LEAF)).fetchone()[0]
    projected = store.memory.read(uri)
    assert '<!-- block:old-a -->' in projected and 'merged-1' not in projected


# ---------------------------------------------------------------------------
# Full A->B->C chain: two jobs sharing one leaf through the publish service.
# ---------------------------------------------------------------------------

@pytest.fixture
def v4_jobs(real_store, monkeypatch):
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.setenv('SAM_MD_READ_PROTOCOL', 'v4')
    for job_id in (JOB, JOB2):
        real_store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (job_id, COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    real_store.db.commit()
    return real_store


def kv_key(job_id):
    return f'sam://kv/rt/md-direct/{job_id}'


def load_state(store, job_id):
    row = store.db.execute('SELECT content FROM memory_kv WHERE uri=?', (kv_key(job_id),)).fetchone()
    return json.loads(row['content'])


def kv_save(store, state, job_id):
    store.db.execute('INSERT INTO memory_kv(uri,content,metadata) VALUES(?,?,?) '
                     'ON CONFLICT(uri) DO UPDATE SET content=excluded.content',
                     (kv_key(job_id), json.dumps(state, ensure_ascii=False), '{}'))
    store.db.commit()


def direct_publish(store, model, state, job_id, save=None):
    def call(action, payload):
        return model.execute(skill_text='stage-c-skill',
            text=json.dumps({'action': action, **payload}, ensure_ascii=False))

    service = NotesPublishService(store.memory.database, store.memory, dossier(),
        call=call, save=save or (lambda: kv_save(store, state, job_id)),
        original_name='sample.docx')
    service.run(company=COMPANY, job_id=job_id, state=state)
    return service


class SeedBothModel(PublishModel):
    """Job 1: both notes land on L-bizov-07 as two ordinary blocks."""
    leaf_for = {'n-0001': LEAF, 'n-0002': LEAF}


class MergeModel(PublishModel):
    """Job 2: merge the two job-1 blocks on the target leaf, carry every old
    evidence ref and attach this job's fresh note as new evidence."""
    target_leaf = LEAF
    merge_reason = '员工判断两旧块同主题，合并呈现且事实证据全量保留'

    def submit_leaf(self, request):
        if request['target']['leaf_code'] != self.target_leaf:
            return super().submit_leaf(request)
        current = request['current']
        covered_ids = ('pub-grp-n-0001', 'pub-grp-n-0002')
        blocks = [deepcopy(b) for b in current['blocks'] if b['block_id'] not in covered_ids]
        carry = [dict(from_block_id=b['block_id'], from_ref=e['ref'], to_ref='m-' + e['ref'])
                 for b in current['blocks'] if b['block_id'] in covered_ids
                 for e in b['evidence']]
        group = request['groups'][0]
        member = group['members'][0]
        grant = next(g for g in request['grants']
                     if g['grant_id'] == member['primary_ref']['grant_id'])
        merge = dict(block_id='merged-cap',
            text='合并两段既有陈述：技术描述可回查，有9个[^m-a]；参数值为9[^m-b]。'
                 '\n\n本轮新读证据：有9个[^m-new]。',
            covers=list(covered_ids), carry=carry,
            new_evidence=[dict(ref='m-new', grant_id=grant['grant_id'],
                file_hash=grant['file_hash'], original_name=grant['original_name'],
                source_loc=grant['source_loc'],
                start_line=member['primary_ref']['start_line'],
                end_line=member['primary_ref']['end_line'],
                fact_refs=[group['group_ref']], note_id=member['note_id'])],
            reason=self.merge_reason)
        return dict(leaf_code=request['target']['leaf_code'],
            base_version=current['version'], base_sha=current['content_sha'],
            blocks=blocks, conflicts=[], merges=[merge],
            note='员工发布说明：同叶合并两旧块并补本轮新读证据')


def test_full_chain_merge_happy_path_and_replay_zero_call(v4_jobs):
    store = v4_jobs
    first = reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB,
                                               limits=PUBLISH_LIMITS)
    assert first['done']
    before = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert [b['block_id'] for b in before['blocks']] == ['pub-grp-n-0001', 'pub-grp-n-0002']
    model = MergeModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert result['done'] and result['mapped_notes']['publication']['complete']
    assert result['calls']['publish'] == 3  # one assignment + two leaf turns
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert current['version'] == 2
    assert [b['block_id'] for b in current['blocks']] == ['merged-cap']
    merged = current['blocks'][0]
    old_refs = {e['ref']: e for b in before['blocks'] for e in b['evidence']}
    by_ref = {e['ref']: e for e in merged['evidence']}
    assert by_ref['m-a'] == dict(old_refs['a'], ref='m-a')
    assert by_ref['m-b'] == dict(old_refs['b'], ref='m-b')
    publications = store.db.execute(
        'SELECT job_id,note_id,fact_id FROM md_note_publications').fetchall()
    job1_facts = {r['fact_id'] for r in publications if r['job_id'] == JOB}
    job2_new = {r['fact_id'] for r in publications
                if r['job_id'] == JOB2 and r['note_id'] == 'n-0001'}
    assert {i for e in merged['evidence'] for i in e['fact_ids']} == job1_facts | job2_new
    # Same-transaction rewrite audit: full old/new markdown+blocks+coverage+reason.
    rows = store.db.execute('SELECT * FROM md_leaf_rewrites').fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row['operation_id'].startswith('md-') and row['leaf_code'] == LEAF
    assert row['base_version'] == 1 and row['base_sha'] == before['content_sha']
    assert row['old_markdown'] == before['markdown'] and row['new_markdown'] == current['markdown']
    assert json.loads(row['old_blocks']) == before['blocks']
    assert json.loads(row['new_blocks']) == current['blocks']
    assert json.loads(row['coverage']) == [dict(block_id='merged-cap',
        covers=['pub-grp-n-0001', 'pub-grp-n-0002'],
        carry=[dict(from_block_id='pub-grp-n-0001', from_ref='a', to_ref='m-a'),
               dict(from_block_id='pub-grp-n-0002', from_ref='b', to_ref='m-b')],
        reason=MergeModel.merge_reason)]
    assert store.db.execute('SELECT count(*) FROM md_leaf_operations').fetchone()[0] == 3
    # Full replay of BOTH jobs: durable receipts make every stage a zero-call no-op.
    replay1 = SeedBothModel()
    again1 = reader(store, replay1).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    assert again1['done'] and replay1.requests == []
    replay2 = MergeModel()
    again2 = reader(store, replay2).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert again2['done'] and replay2.requests == []
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5


class BadMergeThenAppendModel(MergeModel):
    """First draft drops one carry item; the corrected draft gives up merging
    and falls back to verbatim retention plus a plain append."""

    def submit_leaf(self, request):
        if request['target']['leaf_code'] != LEAF:
            return super().submit_leaf(request)
        if 'correction_feedback' not in request:
            bad = super().submit_leaf(request)
            bad['merges'][0]['carry'] = bad['merges'][0]['carry'][:1]
            return bad
        current = request['current']
        blocks = [deepcopy(b) for b in current['blocks']]
        group = request['groups'][0]
        member = group['members'][0]
        grant = next(g for g in request['grants']
                     if g['grant_id'] == member['primary_ref']['grant_id'])
        blocks.append(dict(block_id='pub2-grp-n-0001', text='本轮新读证据：有9个[^c]。',
            evidence=[dict(ref='c', grant_id=grant['grant_id'],
                file_hash=grant['file_hash'], original_name=grant['original_name'],
                source_loc=grant['source_loc'],
                start_line=member['primary_ref']['start_line'],
                end_line=member['primary_ref']['end_line'],
                fact_refs=[group['group_ref']], note_id=member['note_id'])]))
        return dict(leaf_code=request['target']['leaf_code'],
            base_version=current['version'], base_sha=current['content_sha'],
            blocks=blocks, conflicts=[],
            note='员工发布说明：合并校验失败，退回逐字保留加普通追加')


def test_failed_merge_falls_back_to_verbatim_retention_and_plain_append(v4_jobs):
    store = v4_jobs
    reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    model = BadMergeThenAppendModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert result['done']
    item = result['mapped_notes']['publication']['items'][LEAF]
    assert item['status'] == 'submitted' and item['correction_round'] == 1
    assert 'carried exactly once' in item['rejected_drafts'][0]['reason']
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert [b['block_id'] for b in current['blocks']] == \
        ['pub-grp-n-0001', 'pub-grp-n-0002', 'pub2-grp-n-0001']
    # The failed merge attempt committed nothing: no audit row, no extra fact.
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5


class BadNewEvidenceThenGoodModel(MergeModel):
    """First draft carries a non-array new_evidence value; the corrected draft
    is the ordinary good merge. The host must fail closed, never coerce."""
    bad_value = None

    def submit_leaf(self, request):
        if request['target']['leaf_code'] != LEAF:
            return super().submit_leaf(request)
        draft = super().submit_leaf(request)
        if 'correction_feedback' not in request:
            draft['merges'][0]['new_evidence'] = self.bad_value
        return draft


@pytest.mark.parametrize('value', NEW_EVIDENCE_BAD_VALUES,
                         ids=['null', 'object', 'string', 'zero', 'false'])
def test_merge_new_evidence_wrong_type_rejected_service(v4_jobs, value):
    store = v4_jobs
    reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    model = BadNewEvidenceThenGoodModel()
    model.bad_value = value
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert result['done']
    item = result['mapped_notes']['publication']['items'][LEAF]
    assert item['status'] == 'submitted' and item['correction_round'] == 1
    assert 'new_evidence must be an array' in item['rejected_drafts'][0]['reason']
    # The failed attempt committed nothing; the corrected merge landed exactly once.
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert [b['block_id'] for b in current['blocks']] == ['merged-cap']


class MergeCitesViaBlockModel(MergeModel):
    """The job-2 fact is cited by a plain appended block; the merge itself
    carries only old evidence (new_evidence omitted or an explicit [])."""
    new_evidence_case = 'omit'

    def submit_leaf(self, request):
        if request['target']['leaf_code'] != LEAF:
            return super().submit_leaf(request)
        draft = super().submit_leaf(request)
        merge = draft['merges'][0]
        plain = dict(block_id='pub2-grp-n-0001', text='本轮新读证据：有9个[^n]。',
                     evidence=[dict(merge['new_evidence'][0], ref='n')])
        if self.new_evidence_case == 'omit':
            del merge['new_evidence']
        else:
            merge['new_evidence'] = []
        merge['text'] = '合并两段既有陈述：技术描述可回查，有9个[^m-a]；参数值为9[^m-b]。'
        draft['blocks'] = draft['blocks'] + [plain]
        return draft


@pytest.mark.parametrize('case', ['omit', 'empty'], ids=['absent', 'empty-array'])
def test_merge_new_evidence_absent_or_empty_array_allowed_service(v4_jobs, case):
    store = v4_jobs
    reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    model = MergeCitesViaBlockModel()
    model.new_evidence_case = case
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert result['done'] and result['mapped_notes']['publication']['complete']
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert [b['block_id'] for b in current['blocks']] == ['merged-cap', 'pub2-grp-n-0001']
    merged = current['blocks'][0]
    assert {e['ref'] for e in merged['evidence']} == {'m-a', 'm-b'}
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5


def external_bump_current(store, *, operation_id='md-external-bump-v22'):
    """A concurrent publisher advances L-bizov-07 outside the publication job."""
    ext = LeafPublishService(store.memory.database, store.memory, company_id=COMPANY,
        quote_resolver=DossierQuoteResolver({SHA: dossier().document}))
    try:
        publisher = MDLeafPublisher(ext, reading_protocol=2)
        current = publisher.current(COMPANY, LEAF)
        blocks = [deepcopy(b) for b in current['blocks']]
        blocks.append(dict(block_id='ext-base', text='外部基线陈述[^x]。',
            evidence=[dict(ref='x', grant_id='0', file_hash=SHA, original_name='sample.docx',
                source_loc=LOC0, start_line=1, end_line=1, fact_ids=[])]))
        windows = MDWindows(dossier())
        grant = windows.grant(windows.windows[0], 'sample.docx')
        grant['company_id'] = COMPANY
        publisher.submit(company=COMPANY, code=LEAF, operation_id=operation_id,
            base_version=current['version'], base_sha=current['content_sha'],
            blocks=blocks, conflicts=[], grants={'0': grant}, job_id='job-ext',
            note='外部并发发布')
    finally:
        ext.close()


def test_base_change_voids_old_covers_rebuilt_against_new_base(v4_jobs):
    store = v4_jobs
    reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    model = MergeModel()
    stage = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=REVIEW_LIMITS)
    assert stage['done'] and stage['mapped_notes']['awaiting_publication']
    state = load_state(store, JOB2)
    saves = {'count': 0}

    def hook_save():
        saves['count'] += 1
        kv_save(store, state, JOB2)
        if saves['count'] == 3:
            # Assignment + base read + draft persisted; a concurrent publisher
            # then advances the leaf before our commit.
            external_bump_current(store)

    direct_publish(store, model, state, JOB2, save=hook_save)
    publication = state['mapped_notes']['publication']
    assert publication['complete']
    item = publication['items'][LEAF]
    assert item['base_changes'] == 1 and item['status'] == 'submitted'
    compares = [r for r in model.publish_requests if r['operation'] == 'compare_submit'
                and r['target']['leaf_code'] == LEAF]
    assert len(compares) == 2 and compares[1]['current']['version'] == 2
    # The resubmission rebuilt covers/carry against the new base and retained
    # the concurrent block; only the successful attempt wrote an audit row.
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert current['version'] == 3
    assert [b['block_id'] for b in current['blocks']] == ['merged-cap', 'ext-base']
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1


def test_projection_failure_rolls_back_whole_publication(v4_jobs, monkeypatch):
    store = v4_jobs
    reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    baseline = registry_counts(store)

    def fail(texts):
        raise RuntimeError('injected embedding failure')

    monkeypatch.setattr(store.memory.embedding, 'embed_texts', fail)
    with pytest.raises(ProjectionError):
        reader(store, MergeModel()).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_note_publications').fetchone()[0] == 2
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0
    assert registry_counts(store) == baseline
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert current['version'] == 1
    assert [b['block_id'] for b in current['blocks']] == ['pub-grp-n-0001', 'pub-grp-n-0002']
    job = store.db.execute('SELECT status FROM parse_jobs WHERE job_id=?', (JOB2,)).fetchone()
    assert job['status'] == 'partial'


def test_merge_employee_without_publish_budget_stops_at_stage_b(v4_jobs):
    store = v4_jobs
    reader(store, SeedBothModel()).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    model = MergeModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=REVIEW_LIMITS)
    assert result['done'] and result['mapped_notes']['awaiting_publication']
    assert not result['mapped_notes'].get('publication')
    assert model.publish_requests == []
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 3
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 0
    assert MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)['version'] == 1


# ---------------------------------------------------------------------------
# CHG-036: carried old facts must keep their current leaf attribution.
#
# The base leaf is seeded directly through MDLeafPublisher, so its facts have
# no identity/placement ledger rows until the test registers them. Every
# mutation below (identity registration, placement, legal reassignment,
# revision append) uses only the public store APIs on the isolated temporary
# ledger; ledger states no legal API can produce (inactive identity, corrupt
# history, stale binding) are simulated by mocking the verified read seam,
# never by weakening production guards.
# ---------------------------------------------------------------------------

class SeededBaseMergeModel(PublishModel):
    """Job 2: merge the two directly-seeded base blocks old-a/old-b on LEAF,
    carrying every old evidence ref and attaching this job's fresh note."""
    merge_reason = '员工判断两旧块同主题，合并呈现且事实证据全量保留'

    def submit_leaf(self, request):
        if request['target']['leaf_code'] != LEAF:
            return super().submit_leaf(request)
        current = request['current']
        covered = ('old-a', 'old-b')
        blocks = [deepcopy(b) for b in current['blocks'] if b['block_id'] not in covered]
        carry = [dict(from_block_id=b['block_id'], from_ref=e['ref'], to_ref='m-' + e['ref'])
                 for b in current['blocks'] if b['block_id'] in covered
                 for e in b['evidence']]
        group = request['groups'][0]
        member = group['members'][0]
        grant = next(g for g in request['grants']
                     if g['grant_id'] == member['primary_ref']['grant_id'])
        merge = dict(block_id='merged-cap',
            text='合并两段既有陈述：技术描述可回查，有9个[^m-a]；参数值为9[^m-b]。'
                 '\n\n本轮新读证据：有9个[^m-new]。',
            covers=list(covered), carry=carry,
            new_evidence=[dict(ref='m-new', grant_id=grant['grant_id'],
                file_hash=grant['file_hash'], original_name=grant['original_name'],
                source_loc=grant['source_loc'],
                start_line=member['primary_ref']['start_line'],
                end_line=member['primary_ref']['end_line'],
                fact_refs=[group['group_ref']], note_id=member['note_id'])],
            reason=self.merge_reason)
        return dict(leaf_code=LEAF,
            base_version=current['version'], base_sha=current['content_sha'],
            blocks=blocks, conflicts=[], merges=[merge],
            note='员工发布说明：同叶合并两旧块并补本轮新读证据')


def attribution_snapshot(store):
    def count(table):
        exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (table,)).fetchone()
        return store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] if exists else 0
    snapshot = {table: count(table) for table in
                ('facts', 'fact_identities', 'fact_revisions', 'fact_placements',
                 'fact_current_placement', 'md_leaf_rewrites', 'md_leaf_operations',
                 'md_note_publications')}
    snapshot.update(registry_counts(store))
    return snapshot


def identity_placements(store):
    return (FactIdentityStore(store.db, company_id=COMPANY),
            FactPlacementStore(store.db, company_id=COMPANY))


def place_on_leaf(store, fact_id, *, leaf=LEAF, trunk='bizov'):
    identities, placements = identity_placements(store)
    identities.register_initial(COMPANY, fact_id, 'test-actor', '测试登记事实身份')
    placements.place_initial(COMPANY, fact_id, trunk, leaf, 'test-actor',
                             '测试登记唯一主叶归属')


def run_seeded_merge(store):
    return reader(store, SeededBaseMergeModel()).run(company_id=COMPANY, job_id=JOB2,
                                                    limits=PUBLISH_LIMITS)


def assert_merge_rejected_clean(store, snapshot, *, version=1):
    """The rejection rolled the whole leaf transaction back: leaf version,
    facts, identities, placements, sources, rewrite audit, operations and
    note receipts are exactly as before the attempt."""
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert current['version'] == version
    assert [b['block_id'] for b in current['blocks']] == ['old-a', 'old-b']
    assert attribution_snapshot(store) == snapshot
    assert store.db.execute('SELECT count(*) FROM md_note_publications WHERE job_id=?',
                            (JOB2,)).fetchone()[0] == 0
    job = store.db.execute('SELECT status FROM parse_jobs WHERE job_id=?', (JOB2,)).fetchone()
    assert job['status'] == 'partial'


def test_merge_with_attributed_carried_facts_passes_and_replays(v4_jobs, pub):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    place_on_leaf(store, f1)
    place_on_leaf(store, f2)
    model = SeededBaseMergeModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert result['done'] and result['mapped_notes']['publication']['complete']
    assert result['calls']['publish'] == 3  # one assignment + two leaf turns
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, LEAF)
    assert current['version'] == 2
    assert [b['block_id'] for b in current['blocks']] == ['merged-cap']
    assert {e['ref'] for e in current['blocks'][0]['evidence']} == {'m-a', 'm-b', 'm-new'}
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1
    # The carried facts keep their first placements; only the two job-2 facts
    # added identity/placement rows.
    pointers = {r['fact_row_id']: r for r in store.db.execute(
        'SELECT * FROM fact_current_placement WHERE company_id=?', (COMPANY,)).fetchall()}
    assert pointers[f1]['leaf_code'] == LEAF and pointers[f1]['placement_seq'] == 1
    assert pointers[f2]['leaf_code'] == LEAF and pointers[f2]['placement_seq'] == 1
    assert store.db.execute('SELECT count(*) FROM fact_current_placement '
                            'WHERE company_id=?', (COMPANY,)).fetchone()[0] == 4
    # Full replay: durable receipts make the whole job a zero-call no-op.
    replay = SeededBaseMergeModel()
    again = reader(store, replay).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert again['done'] and replay.requests == []
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5


def test_merge_rejected_when_carried_fact_has_no_identity(v4_jobs, pub):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    place_on_leaf(store, f2)  # f1 stays unmapped: the merge must refuse it
    snapshot = attribution_snapshot(store)
    with pytest.raises(PublishRejected, match='has no fact identity'):
        run_seeded_merge(store)
    assert_merge_rejected_clean(store, snapshot)


def test_merge_rejected_when_carried_fact_has_no_placement(v4_jobs, pub):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    identities, _ = identity_placements(store)
    identities.register_initial(COMPANY, f1, 'test-actor', '测试登记事实身份')
    place_on_leaf(store, f2)  # f1 has an active identity but was never placed
    snapshot = attribution_snapshot(store)
    with pytest.raises(PublishRejected, match='has no current leaf placement'):
        run_seeded_merge(store)
    assert_merge_rejected_clean(store, snapshot)


def test_merge_rejected_when_carried_fact_reassigned_to_other_leaf(v4_jobs, pub):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    place_on_leaf(store, f1)
    place_on_leaf(store, f2)
    _, placements = identity_placements(store)
    result = placements.reassign(COMPANY, f1, 'bizov', 'L-bizov-08', 'test-actor',
                                 '测试将旧事实合法重归属到另一叶', expected_seq=1)
    assert result['placement_seq'] == 2 and result['leaf_code'] == 'L-bizov-08'
    snapshot = attribution_snapshot(store)
    with pytest.raises(PublishRejected, match='currently placed on'):
        run_seeded_merge(store)
    assert_merge_rejected_clean(store, snapshot)
    # The legal reassignment itself is preserved, never rewritten back.
    pointer = store.db.execute('SELECT * FROM fact_current_placement WHERE fact_row_id=?',
                               (f1,)).fetchone()
    assert pointer['leaf_code'] == 'L-bizov-08' and pointer['placement_seq'] == 2
    history = store.db.execute('SELECT count(*) FROM fact_placements WHERE fact_row_id=?',
                               (f1,)).fetchone()[0]
    assert history == 2


def test_merge_rejected_when_carried_fact_row_is_stale_revision(v4_jobs, pub):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    place_on_leaf(store, f1)
    place_on_leaf(store, f2)
    identities, _ = identity_placements(store)
    fact_uid = identities.identity_for_row(COMPANY, f1)['fact_uid']
    columns = [row[1] for row in store.db.execute('PRAGMA table_info(facts)').fetchall()
               if row[1] != 'id']
    cursor = store.db.execute(
        f"INSERT INTO facts({','.join(columns)}) SELECT {','.join(columns)} "
        'FROM facts WHERE id=?', (f1,))
    new_row = int(cursor.lastrowid)
    store.db.commit()
    revision = identities.append_revision(COMPANY, fact_uid, new_row, 'test-actor',
                                          '测试追加事实新版本')
    assert revision['fact_row_id'] == new_row and revision['fact_version'] == 2
    snapshot = attribution_snapshot(store)
    with pytest.raises(PublishRejected, match='not the current revision'):
        run_seeded_merge(store)
    assert_merge_rejected_clean(store, snapshot)


LEDGER_FAILURES = [
    "fact identity 'fact:broken' is not active",
    'placement history has a sequence gap',
]


@pytest.mark.parametrize('message', LEDGER_FAILURES,
                         ids=['inactive-identity', 'corrupt-history'])
def test_merge_rejected_when_attribution_ledger_fails(v4_jobs, pub, monkeypatch, message):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    place_on_leaf(store, f1)
    place_on_leaf(store, f2)

    def fail(self, company_id, fact_uid):
        raise ValueError(message)

    monkeypatch.setattr(FactPlacementStore, 'resolve_current_identity', fail)
    snapshot = attribution_snapshot(store)
    with pytest.raises(PublishRejected, match='attribution ledger verification failed'):
        run_seeded_merge(store)
    assert_merge_rejected_clean(store, snapshot)


def test_merge_rejected_when_placement_binding_is_not_current(v4_jobs, pub, monkeypatch):
    store = v4_jobs
    _, make = pub
    _, _, (f1, f2) = seeded_leaf(store, make)
    place_on_leaf(store, f1)
    place_on_leaf(store, f2)
    original = FactPlacementStore.resolve_current_identity

    def stale_binding(self, company_id, fact_uid):
        resolved = original(self, company_id, fact_uid)
        if resolved is not None and resolved['placement'] is not None:
            resolved = dict(resolved, placement=dict(
                resolved['placement'],
                binding_revision=resolved['placement']['binding_revision'] + 1))
        return resolved

    monkeypatch.setattr(FactPlacementStore, 'resolve_current_identity', stale_binding)
    snapshot = attribution_snapshot(store)
    with pytest.raises(PublishRejected, match='differs from the current company binding'):
        run_seeded_merge(store)
    assert_merge_rejected_clean(store, snapshot)


# ---------------------------------------------------------------------------
# Unreleased generic-v2 draft pack: two isolated V4 jobs share one finance
# leaf — job 1 lands two ordinary blocks, job 2 merges them in place. The
# draft binding and the packed-URI projection guard are opened through
# test-scoped seams only (monkeypatch, restored before the formal-gate
# assertions); production code, the release set and the SAM_FACTS_TREE
# default stay untouched.
# ---------------------------------------------------------------------------

GENERIC_V2_LEAF = 'L-finance-01'
GENERIC_V2_APPEND_LEAF = 'L-finance-02'


@pytest.fixture
def v4_jobs_generic_v2(tmp_path, monkeypatch):
    """Isolated synthetic company with two V4 jobs and NO binding yet; the
    test injects the draft generic-v2 binding in test scope only."""
    monkeypatch.setattr(settings, 'SAM_FACTS_TREE', True)
    monkeypatch.setenv('SAM_MD_READ_PROTOCOL', 'v4')
    memory = migrated_memory(tmp_path / 'ledger-gv2-merge.db', embedding=Embedding(),
                             company_id=COMPANY)
    store = LeafPublishService(memory.database, memory, company_id=COMPANY)
    store.db.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                     (SHA, 'sample.docx', 1, '/unused', 'now'))
    store.db.execute('''INSERT INTO facts(company_id,entity,attribute,period,value,unit,source_file,module_key,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (COMPANY, '临时主体', '测试指标', '2025-01', '9', '个', SHA, 'finance', 'now'))
    for job_id in (JOB, JOB2):
        store.db.execute('''INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (job_id, COMPANY, SHA, 'sample.docx', 'docx', 'docx', 1, 'now', 'now'))
    store.db.commit()
    yield store
    store.close()
    memory.close()


class GenericV2SeedBothModel(PublishModel):
    """Job 1: both notes land on the same generic-v2 finance leaf."""
    leaf_for = {'n-0001': GENERIC_V2_LEAF, 'n-0002': GENERIC_V2_LEAF}


class GenericV2MergeModel(MergeModel):
    """Job 2: merge the two job-1 blocks on the generic-v2 finance leaf and
    attach this job's fresh note as new evidence; the second note makes a
    plain append on a neighboring finance leaf."""
    target_leaf = GENERIC_V2_LEAF
    leaf_for = {'n-0001': GENERIC_V2_LEAF, 'n-0002': GENERIC_V2_APPEND_LEAF}


def test_generic_v2_same_leaf_merge_two_job_chain_and_replay(v4_jobs_generic_v2, monkeypatch):
    store = v4_jobs_generic_v2
    pack = load_pack('generic-v2')
    # Test-scoped draft binding injection: the formal gate accepts only
    # "generic"; patch it for this single register call, restore immediately,
    # and run both V4 chains under the restored formal gate.
    formal_pack = CompanyBindingStore._pack

    def draft_pack(name):
        return load_pack(name) if name == 'generic-v2' else formal_pack(name)

    monkeypatch.setattr(CompanyBindingStore, '_pack', staticmethod(draft_pack))
    store.binding.register(COMPANY, '临时草稿绑定公司', pack='generic-v2')
    monkeypatch.setattr(CompanyBindingStore, '_pack', staticmethod(formal_pack))

    # The packed-tree URI projection guard is the second production seam that
    # resolves only released packs; allow the draft pack through it for the
    # duration of the chains and replays, then restore it below. Neither
    # bypass is written into any production implementation.
    formal_released = template_packs.load_released_pack
    monkeypatch.setattr(template_packs, 'load_released_pack',
        lambda name='generic': load_pack(name) if name == 'generic-v2' else formal_released(name))

    # Job 1: two ordinary V4 blocks on the same generic-v2 finance leaf.
    seed_model = GenericV2SeedBothModel()
    first = reader(store, seed_model).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    assert first['done'] and first['mapped_notes']['publication']['complete']
    assert first['calls']['publish'] == 2  # one assignment + one shared-leaf turn
    before = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, GENERIC_V2_LEAF)
    assert [b['block_id'] for b in before['blocks']] == ['pub-grp-n-0001', 'pub-grp-n-0002']

    # Job 2: same-leaf merge carrying every old evidence ref plus fresh evidence.
    model = GenericV2MergeModel()
    result = reader(store, model).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert result['done'] and result['mapped_notes']['publication']['complete']
    assert result['calls']['publish'] == 3  # one assignment + two leaf turns
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5
    current = MDLeafPublisher(store, reading_protocol=4).current(COMPANY, GENERIC_V2_LEAF)
    assert current['version'] == 2
    assert [b['block_id'] for b in current['blocks']] == ['merged-cap']
    merged = current['blocks'][0]

    publications = store.db.execute(
        'SELECT job_id,note_id,fact_id,leaf_code FROM md_note_publications').fetchall()
    job1 = {r['note_id']: r for r in publications if r['job_id'] == JOB}
    job2 = {r['note_id']: r for r in publications if r['job_id'] == JOB2}
    assert sorted(job1) == sorted(job2) == ['n-0001', 'n-0002']
    f1, f2 = job1['n-0001']['fact_id'], job1['n-0002']['fact_id']
    new_fact = job2['n-0001']['fact_id']
    assert job1['n-0001']['leaf_code'] == job1['n-0002']['leaf_code'] == GENERIC_V2_LEAF
    assert job2['n-0001']['leaf_code'] == GENERIC_V2_LEAF
    assert job2['n-0002']['leaf_code'] == GENERIC_V2_APPEND_LEAF

    # Every old evidence ref is carried exactly once, host-copied verbatim
    # with only the ref label renamed; the old fact ids stay on their refs.
    old_refs = {e['ref']: e for b in before['blocks'] for e in b['evidence']}
    by_ref = {e['ref']: e for e in merged['evidence']}
    assert by_ref['m-a'] == dict(old_refs['a'], ref='m-a')
    assert by_ref['m-b'] == dict(old_refs['b'], ref='m-b')
    assert by_ref['m-a']['fact_ids'] == [f1] and by_ref['m-b']['fact_ids'] == [f2]
    # The fresh evidence ref resolves to exactly this job's new fact.
    assert by_ref['m-new']['fact_ids'] == [new_fact]
    assert by_ref['m-new']['note_id'] == 'n-0001'
    # Old fact identities are preserved as-is: one active revision row each.
    for old in (f1, f2):
        rows = store.db.execute('SELECT fact_version FROM fact_revisions '
                                'WHERE company_id=? AND fact_row_id=?', (COMPANY, old)).fetchall()
        assert [r['fact_version'] for r in rows] == [1]

    # Attribution: old and new facts sit on the generic-v2 finance leaf under
    # the current binding revision; old placements were never rewritten.
    meta = store.db.execute(
        'SELECT template_pack,pack_version,pack_digest,binding_revision FROM company_tree_meta '
        'WHERE company_id=?', (COMPANY,)).fetchone()
    assert meta['template_pack'] == 'generic-v2' and meta['pack_version'] == pack.version
    assert meta['pack_digest'] == pack.manifest_sha256
    pointers = {r['fact_row_id']: r for r in store.db.execute(
        'SELECT * FROM fact_current_placement WHERE company_id=?', (COMPANY,)).fetchall()}
    assert len(pointers) == 4
    for old in (f1, f2):
        assert pointers[old]['placement_seq'] == 1
    for row in pointers.values():
        expected_leaf = (GENERIC_V2_APPEND_LEAF if row['fact_row_id'] == job2['n-0002']['fact_id']
                         else GENERIC_V2_LEAF)
        assert row['trunk_code'] == 'finance' and row['leaf_code'] == expected_leaf
        assert row['pack_id'] == 'generic-v2' and row['pack_version'] == pack.version
        assert row['manifest_sha256'] == pack.manifest_sha256
        assert row['binding_revision'] == meta['binding_revision']
    history = store.db.execute(
        'SELECT * FROM fact_placements WHERE company_id=?', (COMPANY,)).fetchall()
    assert len(history) == 4
    for row in history:
        assert row['record_type'] == 'initial' and row['placement_seq'] == 1
        assert row['pack_id'] == 'generic-v2' and row['pack_version'] == pack.version
        assert row['manifest_sha256'] == pack.manifest_sha256
        assert row['binding_revision'] == meta['binding_revision']
        assert row['trunk_code'] == 'finance'

    # Same-transaction rewrite audit: full old/new markdown+blocks+coverage.
    rows = store.db.execute('SELECT * FROM md_leaf_rewrites').fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row['operation_id'].startswith('md-') and row['leaf_code'] == GENERIC_V2_LEAF
    assert row['base_version'] == 1 and row['base_sha'] == before['content_sha']
    assert row['old_markdown'] == before['markdown'] and row['new_markdown'] == current['markdown']
    assert json.loads(row['old_blocks']) == before['blocks']
    assert json.loads(row['new_blocks']) == current['blocks']
    assert json.loads(row['coverage']) == [dict(block_id='merged-cap',
        covers=['pub-grp-n-0001', 'pub-grp-n-0002'],
        carry=[dict(from_block_id='pub-grp-n-0001', from_ref='a', to_ref='m-a'),
               dict(from_block_id='pub-grp-n-0002', from_ref='b', to_ref='m-b')],
        reason=MergeModel.merge_reason)]

    # Full replay of BOTH jobs: durable receipts make every stage a zero-call
    # no-op; facts, sources, identities, placements and the leaf audit keep
    # their exact counts (no duplicates).
    snapshot = attribution_snapshot(store)
    replay1 = GenericV2SeedBothModel()
    again1 = reader(store, replay1).run(company_id=COMPANY, job_id=JOB, limits=PUBLISH_LIMITS)
    assert again1['done'] and replay1.requests == []
    replay2 = GenericV2MergeModel()
    again2 = reader(store, replay2).run(company_id=COMPANY, job_id=JOB2, limits=PUBLISH_LIMITS)
    assert again2['done'] and replay2.requests == []
    assert attribution_snapshot(store) == snapshot
    assert store.db.execute('SELECT count(*) FROM facts').fetchone()[0] == 5
    assert store.db.execute('SELECT count(*) FROM md_leaf_rewrites').fetchone()[0] == 1

    # Seams restored: the formal release set still hides generic-v2, formal
    # registration/binding/released-pack loading still refuse it, and the
    # feature gate default stays closed; the injected draft binding is untouched.
    monkeypatch.setattr(template_packs, 'load_released_pack', formal_released)
    assert 'generic-v2' not in available_packs()
    with pytest.raises(ValueError, match='supports only generic'):
        store.binding.register(COMPANY, '草稿门核验公司', pack='generic-v2')
    with pytest.raises(ValueError, match='supports only generic'):
        store.binding.bind_pack(COMPANY, 'generic-v2')
    with pytest.raises(ValueError, match='not released'):
        template_packs.load_released_pack('generic-v2')
    assert store.binding.get(COMPANY)['template_pack'] == 'generic-v2'
    monkeypatch.delenv('SAM_FACTS_TREE', raising=False)
    assert runpy.run_path(settings.__file__)['SAM_FACTS_TREE'] is False
