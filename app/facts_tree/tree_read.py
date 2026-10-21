"""Read-only FactsTree navigation service (F-004-API-001).

Pure reads against the formal ledger and projections: a read-only SQLite
connection, no DDL, no backfill, no model calls, and only the rollup module's
pure render helpers — never publish/consume methods. Every served document is
re-derived deterministically from the hash-verified active leaves and compared
with the stored projection; any divergence fails closed. Each public read runs
inside one read-only SQLite snapshot that begins before its first ledger query
and covers every consistency read of the request.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .. import settings
from ..memory.company_facts.l1_brief import read_optional
from ..memory.paths import FACTS_ROOT, Address, identifier
from ..memory.projection import VectorProjection
from ..storage.archive_paths import hash_relpath
from .fact_placements import FactPlacementStore
from .leaf_store import (DEFINITION, LeafPublishService, LeafRejected, parse_markdown,
                         prose, table_rows)
from .rollup import FactsTreeRollup
from .template_packs import load_pack
from .uris import leaf_uri, tree_doc_uri, trunk_doc_uri


class TreeReadError(Exception):
    """A stable read-failure category plus the HTTP status for the route layer."""

    def __init__(self, category, status, message):
        super().__init__(message)
        self.category = category
        self.status = status


def _consistency(message):
    return TreeReadError('consistency_failed', 409, message)


# V2 structured index line written by md_publish: name, mapped coordinate,
# line range, block id, content hash and fact ids — strict shape, no guessing.
_V2_INDEX = re.compile(
    r'^(?P<name>.+) → (?P<loc>.+) \(lines (?P<start>[0-9]+)-(?P<end>[0-9]+);'
    r' block [^;]+; sha (?P<sha>[0-9a-f]{64}); facts [0-9]+(?:,[0-9]+)*\)$')


def blob_sha256(path, *, chunk_size=1024 * 1024):
    """Streaming content hash; the whole original never enters memory."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_source_ref(content, declared_sources, ref, *, source_names=None):
    """Resolve one footnote label inside already verified leaf content.

    Strict existing formats only: a V2 index line carries the hash directly;
    a V1 definition carries the hash or an exact original name mappable to a
    unique declared hash through ``source_names`` ({file_hash: original_name},
    covering only the leaf's declared sources). An ambiguous or unmappable
    name resolves to no hash — never a guess. Anything malformed is a damaged
    projection and fails closed.
    """
    definitions = {m.group(1): m.group(2).strip() for m in DEFINITION.finditer(content)}
    if ref not in definitions:
        raise TreeReadError('unknown_source', 404,
                            f'source {ref!r} is not in the current leaf index')
    value = definitions[ref]
    match = _V2_INDEX.match(value)
    if match:
        file_hash = match.group('sha')
        if file_hash not in declared_sources:
            raise _consistency(f'source {ref!r} cites a hash outside the declared leaf sources')
        locator = match.group('loc')
        if ':' not in locator:
            raise _consistency(f'source {ref!r} has a malformed index line')
        return {'ref': ref, 'file_hash': file_hash, 'original_name': match.group('name'),
                'locator': locator, 'lines': [int(match.group('start')), int(match.group('end'))]}
    parts = value.replace('→', ' ').rsplit(None, 1)
    if len(parts) != 2 or ':' not in parts[1]:
        raise _consistency(f'source {ref!r} has a malformed index line')
    file, locator = parts[0].strip(), parts[1].strip()
    if file in declared_sources:
        return {'ref': ref, 'file_hash': file,
                'original_name': (source_names or {}).get(file), 'locator': locator, 'lines': None}
    if re.fullmatch(r'[0-9a-f]{64}', file):
        raise _consistency(f'source {ref!r} cites a hash outside the declared leaf sources')
    candidates = [sha for sha in declared_sources if (source_names or {}).get(sha) == file]
    if len(candidates) == 1:
        return {'ref': ref, 'file_hash': candidates[0], 'original_name': file,
                'locator': locator, 'lines': None}
    return {'ref': ref, 'file_hash': None, 'original_name': file,
            'locator': locator, 'lines': None}


def _binding_view(binding):
    return {'template_pack': binding['template_pack'], 'pack_version': binding['pack_version'],
            'binding_revision': binding['binding_revision'], 'pack_digest': binding['pack_digest'],
            'digest_source': binding['digest_source']}


def _leaf_view(leaf):
    return {'code': leaf.code, 'trunk': leaf.trunk, 'title': leaf.title,
            'shape': leaf.shape, 'scope': leaf.scope}


# search_leaves accepts only these exact filter fields. Legacy module_key,
# client company_id and every other key are rejected, never silently dropped
# (a dropped filter would silently widen the search). Fact fields match the
# current readable facts rows of a leaf's formal references exactly — never
# full text, module_key or stale vector metadata.
_SEARCH_FACT_FIELDS = ('period', 'entity', 'attribute', 'unit')
_SEARCH_FILTER_FIELDS = frozenset({'trunk_code', 'leaf_code', *_SEARCH_FACT_FIELDS})
_MAX_FILTER_VALUE = 512

# Decision 13-A: employee-facing reads serve claimed and verified facts only.
# inferred rows, superseded versions and expired facts are refused, and a
# formal leaf reference to one of them fails the whole read closed.
_READABLE_FACT_STATUS = ('claimed', 'verified')


# FactsTree cloud snapshot placement contract (DEC-020 option A, CHG-038).
# The local export validates every indexed fact against the identity and
# placement ledgers before building the dedicated snapshot, then stamps the
# ready section with this minimal marker; the ledger itself never leaves the
# local database. The marker is an integrity and contract-version check, not
# a cryptographic signature — ingestion still relies on the ingest key.
PLACEMENT_CONTRACT_VERSION = 1


def placement_snapshot_digest(section):
    """SHA-256 of the canonical UTF-8 section without its validation marker."""
    body = {key: value for key, value in section.items() if key != 'placement_validation'}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def attach_placement_validation(section):
    """Stamp a fully verified ready section with the placement contract marker."""
    if section.get('state') != 'ready':
        raise ValueError('only a ready facts-tree section can carry placement validation')
    binding = section['tree']['binding']  # the verified binding view of read_tree
    marker = {'contract_version': PLACEMENT_CONTRACT_VERSION,
              'company_id': section['company_id'],
              'template_pack': binding['template_pack'],
              'pack_version': binding['pack_version'],
              'binding_revision': binding['binding_revision'],
              'pack_digest': binding['pack_digest']}
    section['placement_validation'] = {
        **marker, 'snapshot_sha256': placement_snapshot_digest(section)}
    return section


def verify_placement_validation(section, company_id):
    """Fail closed unless a ready section carries its exact contract marker.

    The receiver and the cloud read side recompute the digest and re-check
    the company/binding fields against the snapshot itself; a missing marker,
    an old structure, a mismatch or a damaged section is always refused,
    never served as a fallback.
    """
    marker = section.get('placement_validation')
    if not isinstance(marker, dict):
        raise ValueError('facts-tree snapshot has no placement validation marker')
    if marker.get('contract_version') != PLACEMENT_CONTRACT_VERSION:
        raise ValueError('facts-tree snapshot placement contract version is unsupported')
    if marker.get('company_id') != company_id:
        raise ValueError('placement validation company differs from the host scope')
    tree = section.get('tree')
    binding = tree.get('binding') if isinstance(tree, dict) else None
    if not isinstance(binding, dict):
        raise ValueError('facts-tree snapshot has no verified binding view')
    for field in ('template_pack', 'pack_version', 'binding_revision', 'pack_digest'):
        if marker.get(field) != binding.get(field):
            raise ValueError(
                f'placement validation {field} differs from the snapshot binding')
    if marker.get('snapshot_sha256') != placement_snapshot_digest(section):
        raise ValueError('facts-tree snapshot digest differs from the placement marker')


class TreeReadService:
    """Host-scoped read-only tree/trunk/leaf reads for one bound company."""

    def __init__(self, db_path, memory, *, company_id=None):
        if not settings.SAM_FACTS_TREE:
            raise TreeReadError('facts_tree_disabled', 503, 'facts tree is disabled')
        self.company_id = identifier(company_id) if company_id is not None else None
        try:
            self.store = LeafPublishService.open_readonly(db_path, memory, company_id=self.company_id)
        except LeafRejected as exc:  # the store re-checks the feature gate
            raise TreeReadError('facts_tree_disabled', 503, str(exc)) from exc
        except ValueError as exc:  # binding schema predates the verified baseline
            raise _consistency(f'binding store cannot be verified: {exc}') from exc
        except sqlite3.OperationalError as exc:
            raise TreeReadError('storage_unavailable', 503, str(exc)) from exc
        self.db = self.store.db
        self.db_path = db_path
        self.memory = memory
        # The read-only placement store verifies the ledger schema once here:
        # it refuses to join a caller transaction, so it cannot be built
        # inside the per-request snapshot; its ledger reads join that snapshot
        # instead. A missing or damaged ledger is remembered and fails closed
        # only when a read actually cites facts, exactly as before.
        try:
            self._placements = FactPlacementStore(self.db, company_id=self.company_id,
                                                  read_only=True)
            self._placements_error = None
        except ValueError as exc:
            self._placements = None
            self._placements_error = exc

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @contextmanager
    def _read_snapshot(self):
        """One read-only SQLite snapshot covering a whole read request.

        BEGIN is the request's first statement on this connection; every
        consistency read of the request (binding, leaf index, revisions,
        identity and placement ledgers) joins this single transaction, and
        the private helpers never open a nested one. Any failure rolls back;
        success ends the read-only transaction.
        """
        self.db.execute('BEGIN')
        try:
            yield
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _company(self, company_id):
        company = identifier(company_id)
        if self.company_id is not None and company != self.company_id:
            raise TreeReadError('company_scope_violation', 400,
                                'company differs from the injected host scope')
        return company

    def _bound(self, company_id):
        """The current binding and pack, fully verified, or None when unbound."""
        company = self._company(company_id)
        try:
            binding = self.store.binding.get(company)
        except ValueError as exc:
            raise _consistency(f'binding verification failed: {exc}') from exc
        if binding is None:
            return company, None
        projection = self.store.binding.read_tree_meta(company)
        if projection != {k: v for k, v in binding.items() if k != 'company_id'}:
            raise _consistency('tree.meta projection differs from the current binding')
        pack = load_pack(binding['template_pack'])
        if pack.version != binding['pack_version']:
            raise _consistency('bound pack version unavailable')
        return company, (binding, pack)

    @staticmethod
    def _has_content(sections):
        has_prose = any(prose(sections.get(name, '')).strip() for name in ('概览', '正文', '内容'))
        has_data = bool(table_rows(sections['数据明细'])[1]) if '数据明细' in sections else False
        return has_prose or has_data

    @staticmethod
    def _fact_refs(code, raw):
        """Strict parse of one revision's formal fact references (IDs only)."""
        try:
            refs = json.loads(raw)
        except ValueError as exc:
            raise _consistency(f'leaf {code!r} index fact references are damaged') from exc
        if not isinstance(refs, list) or any(type(ref) is not int or ref < 1 for ref in refs):
            raise _consistency(f'leaf {code!r} index fact references are damaged')
        return set(refs)

    def _verified_leaves(self, company, pack, *, read_content=None):
        """Indexed template leaves cross-checked against revisions and content.

        ``read_content`` defaults to the injected memory port; search_leaves
        injects a reader of the same SQLite file's m1_documents through
        self.db so the whole read stays inside the request's one snapshot.
        """
        read = read_content if read_content is not None else \
            lambda uri: read_optional(self.memory, uri)
        rows = self.db.execute(
            'SELECT leaf_code,uri FROM facts_tree_leaf_index WHERE company_id=? ORDER BY leaf_code',
            (company,)).fetchall()
        leaves = []
        for row in rows:
            code = row['leaf_code']
            if not code.startswith('L-'):
                continue  # conflict documents are never tree/trunk content
            leaf = pack.leaf_for(code)
            if leaf is None:
                raise _consistency(f'indexed leaf {code!r} is outside the bound pack')
            revision = self.db.execute(
                'SELECT version,content_sha,fact_ids FROM leaf_revisions '
                'WHERE company_id=? AND leaf_code=? '
                'ORDER BY version DESC LIMIT 1', (company, code)).fetchone()
            if revision is None:
                raise _consistency(f'indexed leaf {code!r} has no revision')
            content = read(row['uri'])
            if content is None:
                raise _consistency(f'leaf {code!r} projection is missing')
            if hashlib.sha256(content.encode()).hexdigest() != revision['content_sha']:
                raise _consistency(f'leaf {code!r} projection differs from its revision')
            try:
                meta, _, _, sections = parse_markdown(content)
                active = meta['status'] == 'active' and self._has_content(sections)
            except LeafRejected as exc:
                raise _consistency(f'leaf {code!r} projection is invalid: {exc}') from exc
            if (meta['leaf_code'] != code or meta['trunk'] != leaf.trunk
                    or meta['company_id'] != company or meta['version'] != revision['version']):
                raise _consistency(f'leaf {code!r} projection metadata differs from the ledger')
            leaves.append({'code': code, 'uri': row['uri'], 'content': content, 'meta': meta,
                           'sections': sections, 'version': revision['version'],
                           'fact_ids': self._fact_refs(code, revision['fact_ids']),
                           'active': active, 'leaf': leaf})
        return leaves

    def _verified_documents(self, company, pack, leaves):
        """Stored L1/L0 projections must equal the deterministic re-derivation.

        A stored document that disagrees with (or outlives) the active leaves
        fails closed. A missing document while active leaves await the rollup
        is reported as not generated; content is never fabricated here. A
        re-derivation the rollup refuses (Decision 13-A: a formal reference
        no longer resolves to a current claimed/verified fact) maps to the
        same consistency failure — the whole read fails closed, never a
        partial result with the offending fact silently skipped.
        """
        rollup = FactsTreeRollup(self.store)
        active = [(l['uri'], l['content'], l['meta'], l['sections']) for l in leaves if l['active']]
        expected = {}
        briefs, guides = [], []
        for trunk in pack.trunks:
            code = trunk['code']
            try:
                brief, guide = rollup._render_trunk([l for l in active if l[2]['trunk'] == code])
            except LeafRejected as exc:
                raise _consistency(
                    f"trunk {code!r} projections of company {company!r} cannot be "
                    f'rebuilt from the current facts: {exc}') from exc
            expected[trunk_doc_uri(company, pack, code, 'trunk-brief.md')] = brief
            expected[trunk_doc_uri(company, pack, code, 'trunk-guide.md')] = guide
            if brief:
                briefs.append(brief)
            if guide:
                guides.append(guide)
        expected[tree_doc_uri(company, pack, 'tree-brief.md')] = '\n\n'.join(briefs) if briefs else None
        expected[tree_doc_uri(company, pack, 'tree-guide.md')] = '\n\n'.join(guides) if guides else None
        documents = {}
        for uri, content in expected.items():
            stored = read_optional(self.memory, uri)
            if stored is not None and stored != content:
                raise _consistency(f'projection {uri!r} differs from the active leaves')
            documents[uri] = stored
        return documents

    def _company_fact_refs(self, company):
        """Latest-revision fact references of every indexed template leaf.

        IDs only, for the cross-leaf uniqueness proof: out-of-scope leaf
        content is never loaded into the response. A damaged index anywhere
        makes uniqueness unprovable and fails closed; an indexed leaf without
        a latest revision is an inconsistency too — skipping it would leave
        its formal references unaccounted for, so the read fails closed.
        """
        rows = self.db.execute(
            'SELECT leaf_code FROM facts_tree_leaf_index WHERE company_id=? ORDER BY leaf_code',
            (company,)).fetchall()
        refs = {}
        for row in rows:
            code = row['leaf_code']
            if not code.startswith('L-'):
                continue
            revision = self.db.execute(
                'SELECT fact_ids FROM leaf_revisions WHERE company_id=? AND leaf_code=? '
                'ORDER BY version DESC LIMIT 1', (company, code)).fetchone()
            if revision is None:
                raise _consistency(f'indexed leaf {code!r} has no revision')
            refs[code] = self._fact_refs(code, revision['fact_ids'])
        return refs

    def _verify_placements(self, company, binding, scope_entries, all_refs):
        """Fail closed unless every cited fact is exactly placed on its citing leaf.

        DEC-020 option A: the caller's read snapshot covers this whole
        validation and the placement store built at service construction
        joins it, so no transaction is opened here. Each fact ID referenced
        by the formal leaf index of the requested scope must have an active
        identity owned by this company, must BE the current revision of that
        identity, and must have a current unique placement matching the
        company's exact current binding (pack, pack version, manifest digest,
        binding revision) plus the citing trunk and leaf. Cross-leaf
        duplicate references are inconsistencies too. Nothing is guessed from
        module_key or fact_leaf_links, and nothing is written, backfilled or
        repaired here.

        ``all_refs`` is a zero-argument callable for the company-wide
        cross-leaf reference scan; it is only invoked when the requested
        scope cites at least one fact, so an empty-citation scope never
        triggers the scan or the placement ledger queries.
        """
        cited = {}  # fact_row_id -> scope entry, duplicate detection included
        for entry in scope_entries:
            for ref in entry['fact_ids']:
                if ref in cited:
                    raise _consistency(
                        f'fact {ref} is cited by both leaf {cited[ref]["code"]!r} '
                        f'and leaf {entry["code"]!r}')
                cited[ref] = entry
        if not cited:
            return  # no formal fact references in scope: nothing to verify
        all_refs = all_refs()  # uniqueness proof only for cited facts
        for ref, entry in cited.items():
            others = sorted(code for code, refs in all_refs.items()
                            if code != entry['code'] and ref in refs)
            if others:
                raise _consistency(
                    f'fact {ref} is cited by leaf {entry["code"]!r} and also by '
                    + ', '.join(repr(code) for code in others))
        if self._placements is None:  # missing ledger structure fails closed
            raise _consistency(
                f'placement ledger cannot be verified: {self._placements_error}')
        placements = self._placements
        target = (binding['template_pack'], binding['pack_version'],
                  binding['pack_digest'], binding['binding_revision'])
        for ref, entry in sorted(cited.items()):
            where = f'fact {ref} cited by leaf {entry["code"]!r}'
            mapping = self.db.execute(
                'SELECT company_id,fact_uid FROM fact_revisions WHERE fact_row_id=?',
                (ref,)).fetchone()
            if mapping is None:
                raise _consistency(f'{where} has no fact identity')
            if mapping['company_id'] != company:
                raise _consistency(f'{where} belongs to a different company')
            try:
                resolved = placements.resolve_current_identity(company, mapping['fact_uid'])
            except ValueError as exc:
                raise _consistency(
                    f'{where}: attribution ledger verification failed: {exc}') from exc
            if resolved is None:
                raise _consistency(
                    f'{where} resolves to an unknown identity; the ledger is inconsistent')
            if resolved['fact_row_id'] != ref:
                raise _consistency(
                    f'{where} is not the current revision of its identity; '
                    'stale fact references are never served')
            placement = resolved['placement']
            if placement is None:
                raise _consistency(f'{where} has no current leaf placement')
            if (placement['trunk_code'] != entry['trunk']
                    or placement['leaf_code'] != entry['code']):
                raise _consistency(
                    f"{where} is currently placed on {placement['trunk_code']}/"
                    f"{placement['leaf_code']}, not {entry['trunk']}/{entry['code']}")
            placed = (placement['pack_id'], placement['pack_version'],
                      placement['manifest_sha256'], placement['binding_revision'])
            if placed != target:
                raise _consistency(
                    f'{where} placement binding differs from the current company binding')

    @staticmethod
    def _unbound(company, node, code):
        return {'company_id': company, 'binding': None, 'node': node, 'code': code,
                'state': 'unbound'}

    def _fact_view(self, row):
        """Employee-facing metadata of one currently readable fact row (13-A).

        Status and content fields come straight from the ledger row; the
        source locator is the row's own source_file/source_page/source_span
        with the declared original name looked up in source_files. A damaged
        or unresolvable locator stays None — coordinates are never fabricated
        or repaired here.
        """
        where = f"fact {row['id']}"
        if row['status'] not in _READABLE_FACT_STATUS:
            raise _consistency(
                f"{where} has status {row['status']!r}; only claimed/verified "
                'facts are employee-readable')
        if row['superseded_by'] is not None:
            raise _consistency(f'{where} is superseded; stale fact rows are never served')
        if row['valid_to'] is not None:
            raise _consistency(f'{where} is expired; expired facts are never served')
        source = None
        if row['source_file'] is not None:
            name = self.db.execute('SELECT original_name FROM source_files WHERE file_hash=?',
                                   (row['source_file'],)).fetchone()
            source = {'file_hash': row['source_file'],
                      'original_name': None if name is None else name['original_name'],
                      'page': row['source_page'], 'span': row['source_span']}
        return {'fact_id': row['id'], 'status': row['status'], 'entity': row['entity'],
                'attribute': row['attribute'], 'period': row['period'], 'value': row['value'],
                'value_type': row['value_type'], 'unit': row['unit'], 'source': source}

    def _leaf_facts(self, company, code, fact_ids):
        """Per-fact metadata of one ready leaf's current formal references."""
        if not fact_ids:
            return []
        marks = ','.join('?' * len(fact_ids))
        rows = self.db.execute(
            f'SELECT * FROM facts WHERE company_id=? AND id IN ({marks}) ORDER BY id',
            (company, *sorted(fact_ids))).fetchall()
        if len(rows) != len(fact_ids):
            raise _consistency(f"leaf {code!r} formally references a missing fact row")
        return [self._fact_view(row) for row in rows]

    def read_tree(self, company_id):
        with self._read_snapshot():
            return self._read_tree(company_id)

    def _read_tree(self, company_id):
        company, bound = self._bound(company_id)
        if bound is None:
            return self._unbound(company, 'tree', None)
        binding, pack = bound
        leaves = self._verified_leaves(company, pack)
        documents = self._verified_documents(company, pack, leaves)
        entries = [{'code': l['code'], 'trunk': l['leaf'].trunk, 'fact_ids': l['fact_ids']}
                   for l in leaves]
        self._verify_placements(company, binding, entries,
                                lambda: {l['code']: l['fact_ids'] for l in leaves})
        brief = documents[tree_doc_uri(company, pack, 'tree-brief.md')]
        trunks = []
        for trunk in pack.trunks:
            code = trunk['code']
            trunks.append({
                'code': code, 'order': trunk['order'], 'directory': trunk['directory'],
                'state': 'ready' if documents[trunk_doc_uri(company, pack, code, 'trunk-brief.md')]
                         is not None else 'not_generated',
                'leaves': sum(1 for leaf in pack.leaves if leaf.trunk == code),
                'generated_leaves': sum(1 for l in leaves if l['leaf'].trunk == code and l['active'])})
        if brief is not None:
            state = 'ready'
        elif any(l['active'] for l in leaves):
            state = 'not_generated'  # active leaves await the mechanical rollup
        else:
            state = 'empty'
        return {'company_id': company, 'binding': _binding_view(binding), 'node': 'tree',
                'code': None, 'state': state,
                'brief': brief, 'guide': documents[tree_doc_uri(company, pack, 'tree-guide.md')],
                'trunks': trunks}

    def read_trunk(self, company_id, code):
        with self._read_snapshot():
            return self._read_trunk(company_id, code)

    def _read_trunk(self, company_id, code):
        company, bound = self._bound(company_id)
        if bound is None:
            return self._unbound(company, 'trunk', code)
        binding, pack = bound
        trunk = pack.trunk_for(code)
        if trunk is None:
            raise TreeReadError('unknown_trunk', 404, f'trunk {code!r} is not in the bound pack')
        leaves = self._verified_leaves(company, pack)
        documents = self._verified_documents(company, pack, leaves)
        entries = [{'code': l['code'], 'trunk': l['leaf'].trunk, 'fact_ids': l['fact_ids']}
                   for l in leaves if l['leaf'].trunk == code]
        self._verify_placements(company, binding, entries,
                                lambda: {l['code']: l['fact_ids'] for l in leaves})
        by_code = {l['code']: l for l in leaves}
        catalog = []
        for leaf in pack.leaves:
            if leaf.trunk != code:
                continue
            entry = by_code.get(leaf.code)
            catalog.append({**_leaf_view(leaf),
                            'state': 'not_generated' if entry is None else entry['meta']['status'],
                            'version': None if entry is None else entry['version']})
        brief = documents[trunk_doc_uri(company, pack, code, 'trunk-brief.md')]
        return {'company_id': company, 'binding': _binding_view(binding), 'node': 'trunk',
                'code': code, 'state': 'ready' if brief is not None else 'not_generated',
                'brief': brief, 'guide': documents[trunk_doc_uri(company, pack, code, 'trunk-guide.md')],
                'leaves': catalog}

    def read_leaf(self, company_id, code):
        with self._read_snapshot():
            return self._read_leaf(company_id, code)

    def _read_leaf(self, company_id, code):
        company, bound = self._bound(company_id)
        if bound is None:
            return self._unbound(company, 'leaf', code)
        binding, pack = bound
        leaf = pack.leaf_for(code)
        if leaf is None:
            raise TreeReadError('unknown_leaf', 404, f'leaf {code!r} is not in the bound pack')
        row = self.db.execute(
            'SELECT uri FROM facts_tree_leaf_index WHERE company_id=? AND leaf_code=?',
            (company, code)).fetchone()
        if row is None:
            return {'company_id': company, 'binding': _binding_view(binding), 'node': 'leaf',
                    'code': code, 'state': 'not_generated', 'leaf': _leaf_view(leaf),
                    'uri': None, 'version': None, 'leaf_state': None, 'index': None,
                    'content': None, 'facts': []}
        revision = self.db.execute(
            'SELECT version,content_sha,fact_ids FROM leaf_revisions '
            'WHERE company_id=? AND leaf_code=? '
            'ORDER BY version DESC LIMIT 1', (company, code)).fetchone()
        if revision is None:
            raise _consistency(f'indexed leaf {code!r} has no revision')
        content = read_optional(self.memory, row['uri'])
        if content is None:
            raise _consistency(f'leaf {code!r} projection is missing')
        if hashlib.sha256(content.encode()).hexdigest() != revision['content_sha']:
            raise _consistency(f'leaf {code!r} projection differs from its revision')
        try:
            meta, _, _, _ = parse_markdown(content)
        except LeafRejected as exc:
            raise _consistency(f'leaf {code!r} projection is invalid: {exc}') from exc
        if (meta['leaf_code'] != code or meta['trunk'] != leaf.trunk
                or meta['company_id'] != company or meta['version'] != revision['version']):
            raise _consistency(f'leaf {code!r} projection metadata differs from the ledger')
        fact_ids = self._fact_refs(code, revision['fact_ids'])
        self._verify_placements(
            company, binding,
            [{'code': code, 'trunk': leaf.trunk, 'fact_ids': fact_ids}],
            lambda: self._company_fact_refs(company))
        facts = self._leaf_facts(company, code, fact_ids)
        index = {field: str(meta[field]) for field in ('latest_period', 'created_at', 'updated_at')}
        index.update({'leaf_code': meta['leaf_code'], 'trunk': meta['trunk'], 'title': meta['title'],
                      'company_id': meta['company_id'], 'status': meta['status'],
                      'sources': list(meta['sources']), 'source_file_count': meta['source_file_count'],
                      'version': meta['version'], 'owner_skill': meta['owner_skill']})
        return {'company_id': company, 'binding': _binding_view(binding), 'node': 'leaf',
                'code': code, 'state': 'ready', 'leaf': _leaf_view(leaf), 'uri': row['uri'],
                'version': revision['version'], 'leaf_state': meta['status'],
                'index': index, 'content': content, 'facts': facts}

    def read_fact(self, company_id, fact_id):
        with self._read_snapshot():
            return self._read_fact(company_id, fact_id)

    def _read_fact(self, company_id, fact_id):
        """Decision 13-A: one employee-readable fact through the verified tree seam.

        The fact must belong to the injected company scope, be currently
        readable (claimed/verified, not superseded, not expired), resolve to
        the current revision of its own identity, hold a unique current
        placement under the company's exact current binding, and be formally
        referenced by that placement leaf's latest revision. The legacy
        facts.module_key hint is never consulted: ownership comes from the
        identity/placement ledgers only. Any identity, version or placement
        mismatch fails the whole read closed; nothing is guessed, written or
        repaired.
        """
        if type(fact_id) is not int or fact_id < 1:
            raise TreeReadError('invalid_query', 400, 'fact id must be a positive integer')
        company, bound = self._bound(company_id)
        if bound is None:
            raise TreeReadError('unbound_company', 404,
                                'company has no verified template binding')
        binding, pack = bound
        where = f'fact {fact_id}'
        row = self.db.execute('SELECT * FROM facts WHERE id=?', (fact_id,)).fetchone()
        if row is None or row['company_id'] != company:
            raise TreeReadError('unknown_fact', 404,
                                f'fact {fact_id} is not in the company ledger')
        view = self._fact_view(row)
        mapping = self.db.execute(
            'SELECT company_id,fact_uid FROM fact_revisions WHERE fact_row_id=?',
            (fact_id,)).fetchone()
        if mapping is None:
            raise _consistency(f'{where} has no fact identity')
        if mapping['company_id'] != company:
            raise _consistency(f'{where} identity belongs to a different company')
        if self._placements is None:  # missing ledger structure fails closed
            raise _consistency(
                f'placement ledger cannot be verified: {self._placements_error}')
        try:
            resolved = self._placements.resolve_current_identity(company, mapping['fact_uid'])
        except ValueError as exc:
            raise _consistency(
                f'{where}: attribution ledger verification failed: {exc}') from exc
        if resolved is None:
            raise _consistency(
                f'{where} resolves to an unknown identity; the ledger is inconsistent')
        if resolved['fact_row_id'] != fact_id:
            raise _consistency(
                f'{where} is not the current revision of its identity; '
                'stale fact references are never served')
        placement = resolved['placement']
        if placement is None:
            raise _consistency(f'{where} has no current leaf placement')
        leaf = pack.leaf_for(placement['leaf_code'])
        if leaf is None or leaf.trunk != placement['trunk_code']:
            raise _consistency(f'{where} is placed outside the current bound pack')
        placed = (placement['pack_id'], placement['pack_version'],
                  placement['manifest_sha256'], placement['binding_revision'])
        if placed != (binding['template_pack'], binding['pack_version'],
                      binding['pack_digest'], binding['binding_revision']):
            raise _consistency(
                f'{where} placement binding differs from the current company binding')
        revision = self.db.execute(
            'SELECT fact_ids FROM leaf_revisions WHERE company_id=? AND leaf_code=? '
            'ORDER BY version DESC LIMIT 1', (company, leaf.code)).fetchone()
        if revision is None:
            raise _consistency(f"leaf {leaf.code!r} has no revision")
        if fact_id not in self._fact_refs(leaf.code, revision['fact_ids']):
            raise _consistency(f'{where} is not formally referenced by its current leaf')
        return {'company_id': company, 'binding': _binding_view(binding), 'node': 'fact',
                'code': f'F{fact_id}', 'fact': view,
                'leaf': {'code': leaf.code, 'trunk': leaf.trunk, 'title': leaf.title,
                         'uri': leaf_uri(company, pack, leaf)}}

    def _require_joined_memory(self):
        """search_leaves serves m1 content/vectors through its own connection.

        Leaf content and vector candidates must join the request's one
        read-only snapshot, so they are read from m1_documents/m1_chunks of
        the same SQLite file through self.db — never self.memory.read() or
        LocalVectorMemoryProvider.search(), which use other connections. A
        memory port not backed by this exact file (or missing the projection
        tables) is a hard conflict: reported and refused, never approximated
        across connections.
        """
        database = getattr(self.memory, 'database', None)
        embedding = getattr(self.memory, 'embedding', None)
        files = {row[1]: row[2] for row in self.db.execute('PRAGMA database_list')}
        main = files.get('main')
        if (database is None or embedding is None or not main
                or Path(database).resolve() != Path(main).resolve()):
            raise TreeReadError(
                'storage_conflict', 503,
                'leaf search requires the memory store and the tree ledger in one '
                'SQLite file readable through the service connection')
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('m1_documents','m1_chunks')")}
        if tables != {'m1_documents', 'm1_chunks'}:
            raise TreeReadError(
                'storage_conflict', 503,
                'm1_documents/m1_chunks are not readable through the service connection')

    @staticmethod
    def _search_filters(pack, filters):
        """Strict search filter validation against the verified bound pack."""
        if filters is None:
            return {}
        if not isinstance(filters, dict):
            raise TreeReadError('invalid_filter', 400, 'filters must be a mapping')
        unknown = set(filters) - _SEARCH_FILTER_FIELDS
        if unknown:
            raise TreeReadError(
                'invalid_filter', 400,
                'unsupported leaf search filter: ' + ', '.join(sorted(map(repr, unknown))))
        cleaned = {}
        for key, value in filters.items():
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_FILTER_VALUE:
                raise TreeReadError('invalid_filter', 400,
                                    f'filter {key!r} must be nonempty text of at most '
                                    f'{_MAX_FILTER_VALUE} characters')
            cleaned[key] = value
        trunk, code = cleaned.get('trunk_code'), cleaned.get('leaf_code')
        if trunk is not None and pack.trunk_for(trunk) is None:
            raise TreeReadError('unknown_trunk', 404, f'trunk {trunk!r} is not in the bound pack')
        if code is not None:
            leaf = pack.leaf_for(code)
            if leaf is None:
                raise TreeReadError('unknown_leaf', 404, f'leaf {code!r} is not in the bound pack')
            if trunk is not None and leaf.trunk != trunk:
                raise TreeReadError('invalid_filter', 400,
                                    f'leaf {code!r} does not belong to trunk {trunk!r}')
        return cleaned

    def _leaf_matches_facts(self, company, fact_ids, filters):
        """Exact fact-field match on the leaf's current-revision fact rows.

        The leaf qualifies when at least one of its formally referenced facts
        is currently readable (claimed or verified, not superseded, not
        expired) and matches every given fact field exactly. inferred,
        superseded and expired rows never qualify a leaf.
        """
        fields = {key: filters[key] for key in _SEARCH_FACT_FIELDS if key in filters}
        if not fields:
            return True
        if not fact_ids:
            return False
        marks = ','.join('?' * len(fact_ids))
        clauses = ''.join(f' AND {key}=?' for key in fields)
        row = self.db.execute(
            f'SELECT 1 FROM facts WHERE company_id=? AND id IN ({marks})'
            " AND status IN ('claimed','verified')"
            ' AND superseded_by IS NULL AND valid_to IS NULL'
            f'{clauses} LIMIT 1',
            (company, *sorted(fact_ids), *fields.values())).fetchone()
        return row is not None

    def search_leaves(self, company_id, query, *, filters=None, top_k=20):
        """Ranked formal leaf URIs of the bound pack, in one read snapshot.

        DEC-091 option A core read: only formal leaves of the company's exact
        current binding/pack, each verified end to end (latest revision,
        content hash, frontmatter, formal pack URI, fact identity and unique
        current placement) inside this service's single read-only SQLite
        snapshot. A blank query skips vector retrieval and returns the
        filtered URIs sorted; otherwise vector candidates come from
        VectorProjection(self.db, self.memory.embedding) over the filtered
        leaf documents and are deduplicated per URI by best score, tie-broken
        by URI. Any inconsistency fails the whole call: no partial results,
        no fallback pack, and nothing is written, backfilled or repaired.
        """
        with self._read_snapshot():
            return self._search_leaves(company_id, query, filters=filters, top_k=top_k)

    def _search_leaves(self, company_id, query, *, filters, top_k):
        self._require_joined_memory()
        company, bound = self._bound(company_id)
        if bound is None:
            raise TreeReadError('unbound_company', 404,
                                'company has no verified template binding')
        binding, pack = bound
        filters = self._search_filters(pack, filters)
        if not isinstance(query, str) or len(query) > 8192:
            raise TreeReadError('invalid_query', 400,
                                'query must be text of at most 8192 characters')
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise TreeReadError('invalid_query', 400, 'top_k must be between 1 and 100')

        def m1_content(uri):
            row = self.db.execute(
                'SELECT content FROM m1_documents '
                'WHERE company_id=? AND uri=? AND fact_json IS NULL',
                (company, uri)).fetchone()
            return None if row is None else row['content']

        leaves = self._verified_leaves(company, pack, read_content=m1_content)
        for entry in leaves:  # the indexed URI must be the pack's formal leaf URI
            if entry['uri'] != leaf_uri(company, pack, entry['leaf']):
                raise _consistency(
                    f"leaf {entry['code']!r} index URI is not its formal pack URI")
        trunk, code = filters.get('trunk_code'), filters.get('leaf_code')
        scoped = [entry for entry in leaves
                  if (trunk is None or entry['leaf'].trunk == trunk)
                  and (code is None or entry['code'] == code)]
        matched = [entry for entry in scoped
                   if self._leaf_matches_facts(company, entry['fact_ids'], filters)]
        self._verify_placements(
            company, binding,
            [{'code': entry['code'], 'trunk': entry['leaf'].trunk,
              'fact_ids': entry['fact_ids']} for entry in matched],
            lambda: {entry['code']: entry['fact_ids'] for entry in leaves})
        uris = sorted(entry['uri'] for entry in matched)
        if not query.strip() or not uris:
            return uris[:top_k]
        try:
            projection = VectorProjection(self.db, self.memory.embedding)
        except ValueError as exc:
            raise TreeReadError(
                'storage_conflict', 503,
                f'vector projection cannot join the read snapshot: {exc}') from exc
        address = Address(FACTS_ROOT.removesuffix('/'), 'facts', company, '', ())
        rows = projection.search(address, query, top_k=top_k, document_uris=uris)
        best = {}
        for row in rows:
            if row['uri'] not in best or row['score'] > best[row['uri']]:
                best[row['uri']] = row['score']
        ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
        return [uri for uri, _ in ranked[:top_k]]

    def _original_state(self, company, file_hash, objects_path, storage_paths):
        """Company-scoped availability of one cited original; never its bytes.

        The source_files association is re-checked under the same company
        scope conditions as LeafPublishService._sources; a missing or foreign
        association is ledger/projection damage and fails closed. A missing
        blob or a content hash mismatch only marks the original unavailable —
        no download URL is handed out either way.
        """
        scoped = self.db.execute('''SELECT 1 FROM file_classifications WHERE company_id=? AND file_hash=?
            UNION ALL SELECT 1 FROM parse_jobs WHERE company_id=? AND file_hash=?
            UNION ALL SELECT 1 FROM facts WHERE company_id=? AND source_file=? LIMIT 1''',
            (company, file_hash, company, file_hash, company, file_hash)).fetchone()
        if file_hash not in storage_paths or scoped is None:
            raise _consistency('leaf source is missing or outside the company scope')
        path = Path(objects_path) / hash_relpath(file_hash)
        if not path.is_file() and storage_paths[file_hash]:
            path = Path(objects_path) / storage_paths[file_hash]
        if not path.is_file():
            return {'state': 'unavailable', 'reason': 'original_missing'}
        if blob_sha256(path) != file_hash:
            return {'state': 'unavailable', 'reason': 'integrity_failed'}
        return {'state': 'available',
                'download_url': f'/api/knowledge/sources/{file_hash}/original'}

    def read_leaf_source(self, company_id, code, ref, *, objects_path):
        """F-004-API-002: locate one source cited by the current verified leaf.

        The leaf passes the same binding/revision/projection verification as
        API-001 before its index is parsed, and only citations actually
        present in that verified content resolve. Original bytes stay with
        the document cabinet: the response is locator metadata plus a cabinet
        URL or an explicit unavailable state — never bytes, storage paths or
        mapping internals.
        """
        with self._read_snapshot():
            return self._read_leaf_source(company_id, code, ref, objects_path=objects_path)

    def _read_leaf_source(self, company_id, code, ref, *, objects_path):
        leaf = self._read_leaf(company_id, code)
        if leaf['state'] != 'ready':
            return leaf  # unbound / not_generated explicit states
        declared = sorted(set(leaf['index']['sources']))
        rows = self.db.execute(
            'SELECT file_hash,original_name,storage_path FROM source_files WHERE file_hash IN '
            f'({",".join("?" * len(declared))})', declared).fetchall() if declared else []
        source_names = {row['file_hash']: row['original_name'] for row in rows}
        storage_paths = {row['file_hash']: row['storage_path'] for row in rows}
        source = resolve_source_ref(leaf['content'], set(declared), ref, source_names=source_names)
        if source['file_hash'] is None:
            original = {'state': 'unavailable', 'reason': 'source_unresolvable'}
        else:
            original = self._original_state(leaf['company_id'], source['file_hash'],
                                            objects_path, storage_paths)
            source['original_name'] = source_names[source['file_hash']]
        return {'company_id': leaf['company_id'],
                'leaf': {'code': leaf['code'], 'trunk': leaf['leaf']['trunk'],
                         'title': leaf['leaf']['title'], 'version': leaf['version'],
                         'state': leaf['leaf_state']},
                'source': source, 'original': original}

    def snapshot(self, company_id):
        """Company-scoped read-only export: FactsTree documents and navigation only.

        The whole export shares one read-only SQLite snapshot: tree, trunks
        and leaves are read through the private helpers, so no nested
        transaction is opened per node.
        """
        with self._read_snapshot():
            tree = self._read_tree(company_id)
            if tree['state'] == 'unbound':
                return {'company_id': tree['company_id'], 'state': 'unbound'}
            _, (binding, pack) = self._bound(company_id)
            return {'company_id': tree['company_id'], 'state': 'ready', 'tree': tree,
                    'trunks': {trunk['code']: self._read_trunk(company_id, trunk['code'])
                               for trunk in pack.trunks},
                    'leaves': {leaf.code: self._read_leaf(company_id, leaf.code)
                               for leaf in pack.leaves}}
