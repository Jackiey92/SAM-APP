"""Gated mechanical rollups. No trigger, employee call or semantic inference."""
import hashlib

from ..memory.company_facts.l1_brief import projection_lock, publish_pair, read_optional
from ..memory.company_facts import numeric_guard as numbers
from .leaf_store import LeafRejected, enabled, parse_markdown, prose, FOOTNOTE, DEFINITION, numeric_tokens, table_rows
from .uris import tree_doc_uri, trunk_doc_uri


def validate_rollup(markdown, leaves):
    """Every displayed numeric token must occur in an active source leaf."""
    source_texts = []
    for leaf in leaves:
        meta, body, _, _ = parse_markdown(leaf)
        source_texts.extend((str(meta['title']), str(meta['latest_period']), prose(body)))
        source_texts.extend(line.strip() for line in prose(body).splitlines())
        source_texts.extend(cell.strip() for line in prose(body).splitlines() for cell in line.split('|'))
    candidates = [dict(t, period=t.get('period_token') or '')
                  for text in source_texts for t in numeric_tokens(text)]
    for token in numeric_tokens(prose(markdown)):
        if numbers.validate(token['text'].strip() + '{l:leaf}', {'leaf': candidates}, kind='l')[1]:
            raise LeafRejected('rollup number absent from active leaves')


class FactsTreeRollup:
    def __init__(self, leaf_store):
        self.store = leaf_store
        self.memory = leaf_store.memory
        self.root = leaf_store.root

    def _active(self, company, trunk=None):
        self.store._leaf(company, self._first_code(company))
        rows = self.store.db.execute('SELECT leaf_code,uri FROM facts_tree_leaf_index WHERE company_id=? ORDER BY leaf_code', (company,)).fetchall()
        result = []
        for row in rows:
            if not row['leaf_code'].startswith('L-'):
                continue
            content = read_optional(self.memory, row['uri'])
            if content is None:
                continue
            revision = self.store.db.execute('SELECT content_sha FROM leaf_revisions WHERE company_id=? AND leaf_code=? ORDER BY version DESC LIMIT 1',
                                             (company, row['leaf_code'])).fetchone()
            if revision is None or hashlib.sha256(content.encode()).hexdigest() != revision['content_sha']:
                raise LeafRejected('leaf projection differs from revision')
            meta, _, _, sections = parse_markdown(content)
            if meta['status'] != 'active' or (trunk is not None and meta['trunk'] != trunk):
                continue
            has_prose = any(prose(sections.get(name, '')).strip() for name in ('概览', '正文', '内容'))
            has_data = bool(table_rows(sections['数据明细'])[1]) if '数据明细' in sections else False
            if not has_prose and not has_data:
                continue
            result.append((row['uri'], content, meta, sections))
        return result

    def _first_code(self, company):
        return self._pack(company).leaves[0].code

    def _pack(self, company):
        from .template_packs import load_pack
        binding = self.store.binding.get(company)
        if binding is None:
            raise LeafRejected('company has no template binding')
        pack = load_pack(binding['template_pack'])
        if pack.version != binding['pack_version']:
            raise LeafRejected('bound pack version unavailable')
        return pack

    def _publish(self, company, documents, leaves):
        for content in documents.values():
            if content is not None:
                validate_rollup(content, leaves)
        changes = {uri: content for uri, content in documents.items() if read_optional(self.memory, uri) != content}
        if changes:
            self.store._publish_documents(company, changes, metadata={'company_id': company, 'owner_skill': 'fact-leaf'})
        return {'changed': bool(changes), 'uris': list(documents)}

    @staticmethod
    def _excerpt(content, text, code):
        # Namespace citation labels so multiple leaves cannot collide. Original
        # names/coordinates remain byte-for-byte source text.
        refs = set(FOOTNOTE.findall(DEFINITION.sub('', text)))
        definitions = [m.group() for m in DEFINITION.finditer(parse_markdown(content)[1]) if m.group(1) in refs]
        excerpt = DEFINITION.sub('', text).strip()
        if definitions:
            excerpt += '\n\n' + '\n'.join(definitions)
        return FOOTNOTE.sub(lambda m: f'[^{code}-{m.group(1)}]', excerpt)

    def _render_trunk(self, leaves):
        briefs, guides = [], []
        for uri, content, meta, sections in leaves:
            if meta.get('schema_version') == 2:
                brief = f"# {meta['title']}\n\n{sections['内容']}"
                guide = f"[{meta['title']}]({uri})"
                briefs.append(self._excerpt(content, brief, meta['leaf_code']))
                guides.append(guide)
                continue
            brief = f"# {meta['title']}\n\n{sections['概览']}\n\n{sections['正文']}"
            guide = (f"[{meta['title']}]({uri})\n\n"
                     f"{sections['概览'].splitlines()[0] if sections['概览'] else ''}\n\n{meta['latest_period']}")
            briefs.append(self._excerpt(content, brief, meta['leaf_code']))
            guides.append(self._excerpt(content, guide, meta['leaf_code']))
        return ('\n\n'.join(briefs) + '\n', '\n\n'.join(guides) + '\n') if leaves else (None, None)

    def consume_notifications(self, *, company_id, fence=None):
        """Mechanical L1/L0 projection + version ACK in one fenced snapshot."""
        enabled()
        db = self.store.db
        db.execute('CREATE TABLE IF NOT EXISTS md_leaf_consumption ('
                   'company_id TEXT NOT NULL, leaf_code TEXT NOT NULL, version INTEGER NOT NULL, '
                   'PRIMARY KEY(company_id,leaf_code))')
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        try:
            if fence is not None: fence.check(db)
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='md_leaf_notifications'").fetchone()
            if not exists:
                db.rollback(); return dict(consumed=[])
            pending = db.execute('SELECT n.leaf_code,MAX(n.version) AS version FROM md_leaf_notifications n '
                'LEFT JOIN md_leaf_consumption c ON c.company_id=n.company_id AND c.leaf_code=n.leaf_code '
                'WHERE n.company_id=? AND n.version>COALESCE(c.version,0) GROUP BY n.leaf_code', (company_id,)).fetchall()
            if not pending:
                db.rollback(); return dict(consumed=[])
            leaves = self._active(company_id)
            pack = self._pack(company_id)
            documents = {}
            briefs, guides = [], []
            for trunk in pack.trunks:
                code = trunk['code']
                brief, guide = self._render_trunk([l for l in leaves if l[2]['trunk']==code])
                documents[trunk_doc_uri(company_id, pack, code, 'trunk-brief.md')] = brief
                documents[trunk_doc_uri(company_id, pack, code, 'trunk-guide.md')] = guide
                if brief: briefs.append(brief)
                if guide: guides.append(guide)
            documents[tree_doc_uri(company_id, pack, 'tree-brief.md')] = '\n\n'.join(briefs) if briefs else None
            documents[tree_doc_uri(company_id, pack, 'tree-guide.md')] = '\n\n'.join(guides) if guides else None
            self._publish(company_id, documents, [l[1] for l in leaves])
            consumed = []
            for row in pending:
                version = db.execute('SELECT MAX(version) FROM leaf_revisions WHERE company_id=? AND leaf_code=?',
                                     (company_id,row['leaf_code'])).fetchone()[0]
                if version is None or version < row['version']: raise LeafRejected('notification ahead of revision')
                db.execute('INSERT INTO md_leaf_consumption VALUES(?,?,?) ON CONFLICT(company_id,leaf_code) '
                           'DO UPDATE SET version=MAX(version,excluded.version)', (company_id,row['leaf_code'],version))
                consumed.append(dict(leaf_code=row['leaf_code'],version=version))
            if fence is not None: fence.check(db)
            db.commit()
            return dict(consumed=consumed)
        except Exception:
            db.rollback(); raise

    def rollup_trunk(self, *, company_id, trunk):
        enabled()
        pack = self._pack(company_id)
        if pack.trunk_for(trunk) is None:
            raise LeafRejected('unknown trunk')
        with projection_lock(self.root, company_id):
            leaves = self._active(company_id, trunk)
            brief_uri = trunk_doc_uri(company_id, pack, trunk, 'trunk-brief.md')
            guide_uri = trunk_doc_uri(company_id, pack, trunk, 'trunk-guide.md')
            brief, guide = self._render_trunk(leaves)
            return self._publish(company_id, {brief_uri: brief, guide_uri: guide}, [l[1] for l in leaves])

    def rollup_tree(self, *, company_id):
        enabled()
        with projection_lock(self.root, company_id):
            leaves = self._active(company_id)
            pack = self._pack(company_id)
            briefs, guides = [], []
            for trunk in pack.trunks:
                code = trunk['code']
                if not any(l[2]['trunk'] == code for l in leaves):
                    continue
                brief = read_optional(self.memory, trunk_doc_uri(company_id, pack, code, 'trunk-brief.md'))
                guide = read_optional(self.memory, trunk_doc_uri(company_id, pack, code, 'trunk-guide.md'))
                if brief:
                    expected_brief, expected_guide = self._render_trunk([l for l in leaves if l[2]['trunk'] == code])
                    if brief != expected_brief or guide != expected_guide:
                        raise LeafRejected('trunk is stale or differs from active leaves')
                    briefs.append(brief)
                    if guide:
                        guides.append(guide)
            return self._publish(company_id, {
                tree_doc_uri(company_id, pack, 'tree-brief.md'): '\n\n'.join(briefs) if briefs else None,
                tree_doc_uri(company_id, pack, 'tree-guide.md'): '\n\n'.join(guides) if guides else None,
            }, [l[1] for l in leaves])


def rollup_trunk(*, leaf_store, company_id, trunk):
    return FactsTreeRollup(leaf_store).rollup_trunk(company_id=company_id, trunk=trunk)


def rollup_tree(*, leaf_store, company_id):
    return FactsTreeRollup(leaf_store).rollup_tree(company_id=company_id)
