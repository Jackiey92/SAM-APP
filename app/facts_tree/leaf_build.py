"""Opt-in host assembly; publication and all durable writes belong to B1."""
from datetime import datetime, timezone
import json

import yaml

from ..memory.company_facts.l1_brief import projection_lock, read_optional
from .leaf_contract import parse_employee_leaf
from .leaf_store import DEFINITION, HEADING, LeafRejected, markdown_syntax, parse_markdown
from .tree_meta import require_enabled


def _cell(value):
    # Audit cells cannot create rows, columns, headings or footnotes.
    return ' '.join(str(value).split()).replace('|', '&#124;').replace('[', '&#91;')


def _split_definitions(body):
    """Move real footnotes to the footer, leaving fenced employee text intact."""
    matches = list(DEFINITION.finditer(markdown_syntax(body)))
    pieces, start = [], 0
    for match in matches:
        pieces.append(body[start:match.start()])
        start = match.end()
    pieces.append(body[start:])
    return ''.join(pieces).strip(), '\n'.join(body[m.start():m.end()] for m in matches)


class LeafBuildService:
    def __init__(self, leaf_store, *, quote_resolver):
        self.store = leaf_store
        self.quote_resolver = quote_resolver

    def _coordinates(self, company_id, body, source_files):
        sources, names = self.store._sources(company_id, source_files)
        definitions = self.store._footnotes(body, sources, names)
        for sha, loc in definitions.values():
            if self.quote_resolver.resolve(file_hash=sha, loc=loc) is None:
                raise LeafRejected('coordinate unavailable in task evidence')
        return sources

    def _result(self, company_id, code, result):
        row = self.store.db.execute('''SELECT created_at FROM leaf_revisions
            WHERE company_id=? AND leaf_code=? AND version=?''',
            (company_id, code, result['version'])).fetchone()
        return {**result, 'updated_at': row['created_at']}

    def build_leaf(self, *, company_id, leaf_code, employee_payload, job_id=None):
        require_enabled()
        payload = parse_employee_leaf(employee_payload)
        if payload['leaf_code'] != leaf_code:
            raise LeafRejected('employee target mismatch')
        with projection_lock(self.store.root, company_id):
            leaf, pack = self.store._leaf(company_id, leaf_code)
            body = payload['markdown'].replace('\r\n', '\n').strip()
            headings = [m[1].strip() for m in HEADING.finditer(markdown_syntax(body))]
            expected = ['概览', '正文'] + (['数据明细'] if leaf.shape == 's' or '数据明细' in headings else [])
            if body.startswith('---') or headings != expected:
                raise LeafRejected('employee must supply body sections only')
            sources = self._coordinates(company_id, body, payload['source_files'])
            facts = self.store._facts(company_id, leaf, pack, payload['fact_ids'])
            last = self.store.db.execute('''SELECT * FROM leaf_revisions WHERE company_id=?
                AND leaf_code=? ORDER BY version DESC LIMIT 1''', (company_id, leaf_code)).fetchone()
            previous = None
            if last:
                indexed = self.store.db.execute('SELECT uri FROM facts_tree_leaf_index WHERE company_id=? AND leaf_code=?',
                                                (company_id, leaf_code)).fetchone()
                previous = read_optional(self.store.memory, indexed['uri']) if indexed else None
                if previous is None:
                    raise LeafRejected('previous projection unavailable')
            periods = [fact['period'] for fact in facts.values()]
            period = max(periods) if periods and all(isinstance(p, str) and p.strip() for p in periods) else 'unspecified'
            now = datetime.now(timezone.utc).isoformat()
            meta = dict(leaf_code=leaf_code, trunk=leaf.trunk, title=leaf.title, company_id=company_id,
                        schema_version=1, status='active', latest_period=period, sources=sources,
                        source_file_count=len(sources), version=last['version'] + 1 if last else 1,
                        created_at=now, updated_at=now, owner_skill='fact-leaf')
            history = '| 版本 | 时间 | 技能 | 原件 | 摘要 |\n|---|---|---|---|---|'
            if previous:
                old_meta, old_body, _, sections = parse_markdown(previous)
                first = self.store.db.execute('''SELECT created_at FROM leaf_revisions WHERE company_id=?
                    AND leaf_code=? ORDER BY version LIMIT 1''', (company_id, leaf_code)).fetchone()
                meta['created_at'] = first['created_at']
                history, _ = _split_definitions(sections['编辑历史'])
                # Reconstruct the old full document byte-for-byte for a true no-op;
                # timestamps/history must not manufacture a new content hash.
                history_start = next(m.start() for m in HEADING.finditer(markdown_syntax(old_body))
                                     if m[1].strip() == '编辑历史')
                old_employee_body = old_body[:history_start].strip()
                old_definitions = '\n'.join(m[0] for m in DEFINITION.finditer(markdown_syntax(old_body)))
                candidate_body, candidate_definitions = _split_definitions(body)
                if (candidate_body == old_employee_body and candidate_definitions == old_definitions
                        and sources == old_meta['sources'] and period == str(old_meta['latest_period'])
                        and leaf.title == old_meta['title'] and old_meta['status'] == 'active'
                        and sorted(set(payload['fact_ids'])) == json.loads(last['fact_ids'])
                        and payload['note'] == last['change_note']):
                    markdown = previous
                else:
                    markdown = None
            else:
                markdown = None
            if markdown is None:
                history += '\n| v{} | {} | fact-leaf | {} | {} |'.format(
                    meta['version'], now, _cell(', '.join(sources)), _cell(payload['note']))
                employee_body, definitions = _split_definitions(body)
                markdown = ('---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n'
                            + employee_body + '\n\n## 编辑历史\n' + history
                            + '\n\n' + definitions + '\n')
            # Keep resolver injection scoped to this serialized call, not global state.
            resolver = self.store.quote_resolver
            try:
                self.store.quote_resolver = self.quote_resolver
                result = self.store.publish_leaf(company_id=company_id, leaf_code=leaf_code, markdown=markdown,
                    source_files=sources, fact_ids=payload['fact_ids'], change_note=payload['note'], job_id=job_id)
            finally:
                self.store.quote_resolver = resolver
            return self._result(company_id, leaf_code, result)

    def build_conflict(self, **kwargs):
        require_enabled()
        with projection_lock(self.store.root, kwargs['company_id']):
            self._coordinates(kwargs['company_id'], kwargs['markdown'], kwargs['source_files'])
            resolver = self.store.quote_resolver
            try:
                self.store.quote_resolver = self.quote_resolver
                result = self.store.publish_conflict(**kwargs)
            finally:
                self.store.quote_resolver = resolver
            return self._result(kwargs['company_id'], kwargs['leaf_code'], result)
