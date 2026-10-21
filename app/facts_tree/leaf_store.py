"""Opt-in leaf publication. Employee prose is validated, never repaired.

The document port is explicitly injected: B1 does not wire a production
provider or change its retrieval/whitelist contract.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Protocol

import yaml

from ..db.database import connect
from ..memory import paths
from ..memory.company_facts.l1_brief import projection_lock, publish_pair, read_optional
from ..memory.company_facts import numeric_guard as numbers
from ..module_tree import TODO_DIRECTORY
from .company_binding import CompanyBindingStore
from .template_packs import load_pack, _UniqueLoader
from .tree_meta import require_enabled
from .uris import leaf_uri as pack_leaf_uri, leaf_slug


class LeafRejected(ValueError):
    """An unusable upstream artifact, not an infrastructure failure."""


class QuoteResolver(Protocol):
    def resolve(self, *, file_hash: str, loc: str) -> str | None: ...


class NullQuoteResolver:
    def resolve(self, *, file_hash, loc):
        return None


class InMemoryQuoteResolver:
    def __init__(self, quotes=None):
        self.quotes = dict(quotes or {})

    def resolve(self, *, file_hash, loc):
        return self.quotes.get((file_hash, loc))


# Markdown syntax only; no business classification rules.
FOOTNOTE = re.compile(r"\[\^([^\]\s]+)\]")
DEFINITION = re.compile(r"^\[\^([^\]\s]+)\]:\s*(.+)$", re.M)
HEADING = re.compile(r"^##[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.M)
FIELDS = frozenset('leaf_code trunk title company_id status latest_period sources source_file_count version created_at updated_at owner_skill'.split())


def enabled():
    try:
        require_enabled()
    except RuntimeError as exc:
        raise LeafRejected(str(exc)) from exc


def _ends_footnote(paragraph):
    """Only closing punctuation/whitespace may follow the final citation."""
    return bool(re.search(r'(?:\[\^[^\]\s]+\])+[\s。！？；：、）】」』….!?;:)\]}>”’"\']*$', paragraph))


def slug(title):
    """Compatibility wrapper for the shared FactsTree filename rule."""
    return leaf_slug(title)


def markdown_syntax(text):
    """Mask fenced code without changing offsets used for section slicing."""
    result, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence is None and marker:
            fence = marker[1]
        elif fence and marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
            result.append(''.join('\n' if c == '\n' else ' ' for c in line))
            fence = None
            continue
        result.append(''.join('\n' if c == '\n' else ' ' for c in line) if fence else line)
    return ''.join(result)


def parse_markdown(markdown):
    if not isinstance(markdown, str):
        raise LeafRejected('missing frontmatter')
    markdown = markdown.replace('\r\n', '\n')
    if not markdown.startswith('---\n'):
        raise LeafRejected('missing frontmatter')
    try:
        raw, body = markdown[4:].split('\n---\n', 1)
        meta = yaml.load(raw, Loader=_UniqueLoader)
    except (ValueError, yaml.YAMLError, TypeError, RecursionError) as exc:
        raise LeafRejected('invalid frontmatter') from exc
    if not isinstance(meta, dict) or not FIELDS <= meta.keys():
        raise LeafRejected('missing frontmatter fields')
    headings = list(HEADING.finditer(markdown_syntax(body)))
    names = [m.group(1).strip() for m in headings]
    if len(names) != len(set(names)) or set(names) & {'缺口', '待补', '缺失'}:
        raise LeafRejected('invalid section headings')
    sections = {m.group(1).strip(): body[m.end():headings[i+1].start() if i+1 < len(headings) else len(body)].strip()
                for i, m in enumerate(headings)}
    return meta, body, names, sections


def prose(text):
    text = DEFINITION.sub('', text)
    return FOOTNOTE.sub('', text)


def table_rows(text):
    lines = [line.strip() for line in DEFINITION.sub('', text).splitlines() if line.strip()]
    if not lines or any(not line.startswith('|') or not line.endswith('|') for line in lines):
        raise LeafRejected('expected a Markdown table')
    rows = [tuple(c.strip() for c in line[1:-1].split('|')) for line in lines]
    if len(set(rows[0])) != len(rows[0]) or not all(rows[0]):
        raise LeafRejected('ambiguous table header')
    if len(rows) < 2 or len(rows[0]) != len(rows[1]) or any(not re.fullmatch(r':?-+:?', c) for c in rows[1]):
        raise LeafRejected('invalid table header')
    if any(len(row) != len(rows[0]) for row in rows[2:]):
        raise LeafRejected('invalid table row')
    return rows[0], rows[2:]


def history_rows(text):
    _, rows = table_rows(text)
    normalized = {tuple(' '.join(cell.split()) for cell in row) for row in rows}
    if len(normalized) != len(rows):
        raise LeafRejected('duplicate history row')
    return normalized


def numeric_tokens(text):
    """Reuse the existing scanner, preserving periods after Markdown spaces.

    Its NUMBER alternative can start in leading whitespace before PERIOD;
    recover that exact existing PERIOD token rather than treating its suffix
    as an unrelated signed number. No new number/period grammar is added.
    """
    consumed = 0
    for token in numbers.tokens(text):
        if token['start'] < consumed:
            continue
        start = token['start'] + len(token['text']) - len(token['text'].lstrip())
        period = numbers.PERIOD.match(text, start)
        if period and period.end() > token['end']:
            corrected = next(numbers.tokens(text[start:]))
            token = {**corrected, 'start': start + corrected['start'], 'end': start + corrected['end']}
        consumed = token['end']
        yield token


def check_union(text, facts, quotes):
    """Numeric guard tokens, with a fact union or exact quote-token fallback."""
    for token in numeric_tokens(prose(text)):
        literal = token['text'].strip()
        matched_fact = any(not numbers.validate(
            literal + '{f:' + str(ref) + '}', {str(ref): fact}, kind='f')[1]
            for ref, fact in facts.items())
        matched_quote = any(literal == t['text'].strip()
                            for quote in quotes for t in numeric_tokens(quote))
        if not (matched_fact or matched_quote):
            raise LeafRejected(f'number without fact or quote source: literal={literal!r}')


class LeafPublishService:
    def __init__(self, db_path, memory, *, quote_resolver=None, company_id=None, read_only=False):
        enabled()
        self.read_only = read_only
        if read_only:
            self.binding = CompanyBindingStore(db_path, company_id=company_id, read_only=True)
            self.db = self.binding.db
            self.db.row_factory = sqlite3.Row
        else:
            self.db = connect(db_path)
            self.binding = CompanyBindingStore(self.db, company_id=company_id)
        self.memory = memory
        self.root = getattr(memory, 'memory_root', str(db_path))
        self.quote_resolver = quote_resolver or NullQuoteResolver()
        self.company_id = company_id

    @classmethod
    def open_readonly(cls, db_path, memory, *, company_id=None):
        return cls(db_path, memory, company_id=company_id, read_only=True)

    def _require_writable(self):
        if self.read_only:
            raise PermissionError('leaf store is read-only')

    def _joins_memory(self):
        from .. import settings
        from ..memory.provider import LocalVectorMemoryProvider
        if not settings.SAM_FACTS_TREE or not isinstance(self.memory, LocalVectorMemoryProvider):
            return False
        files = {row[1]: row[2] for row in self.db.execute('PRAGMA database_list')}
        return bool(files.get('main') and Path(files['main']).resolve() == self.memory.database.resolve())

    def _publish_documents(self, company, documents, *, metadata):
        if not self._joins_memory():
            publish_pair(self.memory, documents, metadata=metadata)
            return
        self._require_writable()
        if self.db.in_transaction:
            self.memory._publish_documents_join(documents, metadata=metadata, connection=self.db)
        else:
            # Rollups have no ledger transaction of their own.
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.memory._publish_documents_join(documents, metadata=metadata, connection=self.db)

    def close(self):
        self.db.close()

    def _leaf(self, company_id, leaf_code):
        try:
            binding = self.binding.get(company_id)
            if binding is None:
                raise LeafRejected('company has no template binding')
            pack = load_pack(binding['template_pack'])
            if pack.version != binding['pack_version']:
                raise LeafRejected('bound pack version unavailable')
            leaf = pack.leaf_for(leaf_code)
            if leaf is None:
                raise LeafRejected('unknown leaf code')
            return leaf, pack
        except ValueError as exc:
            raise LeafRejected(str(exc)) from exc

    def _sources(self, company, source_files):
        if not isinstance(source_files, list) or any(not isinstance(s, str) for s in source_files):
            raise LeafRejected('invalid source files')
        sources = sorted(set(source_files))
        names = {}
        for sha in sources:
            row = self.db.execute('SELECT original_name FROM source_files WHERE file_hash=?', (sha,)).fetchone()
            scoped = self.db.execute('''SELECT 1 FROM file_classifications WHERE company_id=? AND file_hash=?
                UNION ALL SELECT 1 FROM parse_jobs WHERE company_id=? AND file_hash=?
                UNION ALL SELECT 1 FROM facts WHERE company_id=? AND source_file=? LIMIT 1''',
                (company, sha, company, sha, company, sha)).fetchone()
            if row is None or scoped is None:
                raise LeafRejected('source sha missing or outside company')
            names.setdefault(row['original_name'], []).append(sha)
        return sources, names

    def _facts(self, company, leaf, pack, fact_ids):
        if not isinstance(fact_ids, list) or any(type(ref) is not int or ref < 1 for ref in fact_ids):
            raise LeafRejected('invalid fact ids')
        facts = {}
        by_code = {l.code: l for l in pack.leaves}
        for ref in sorted(set(fact_ids)):
            row = self.db.execute('SELECT * FROM facts WHERE id=?', (ref,)).fetchone()
            # Decision 13A: claimed and verified facts both publish; conflict,
            # inferred and any other status stay rejected. The note->fact gate
            # (verified notes only) is a separate axis in notes_publish.
            if (row is None or row['company_id'] != company
                    or row['status'] not in ('claimed', 'verified')
                    or row['superseded_by'] is not None or row['valid_to'] is not None
                    or row['module_key'] != leaf.trunk):
                raise LeafRejected('invalid or cross-trunk fact reference')
            links = self.db.execute("""SELECT link.* FROM fact_leaf_links AS link
                JOIN facts AS fact ON fact.id=link.fact_id AND fact.company_id=link.company_id
                WHERE fact.id=? AND fact.company_id=?""", (ref, company)).fetchall()
            for link in links:
                linked_leaf = by_code.get(link['leaf_code'])
                if (linked_leaf is None or link['trunk'] != row['module_key']
                        or linked_leaf.trunk != row['module_key'] or link['source_file'] != row['source_file']):
                    raise LeafRejected('invalid fact leaf assignment')
            facts[ref] = dict(row)
        return facts

    def _footnotes(self, body, sources, names):
        definitions = {}
        body = markdown_syntax(body)
        for match in DEFINITION.finditer(body):
            key, value = match.groups()
            if key in definitions:
                raise LeafRejected('duplicate footnote')
            parts = value.replace('→', ' ').rsplit(None, 1)
            if len(parts) != 2 or ':' not in parts[1]:
                raise LeafRejected('invalid footnote coordinate')
            file, loc = parts
            file = file.strip()
            candidates = [file] if file in sources else names.get(file, [])
            if len(candidates) != 1:
                raise LeafRejected(f'footnote source not declared or ambiguous; declared originals: {sorted(names)}')
            definitions[key] = (candidates[0], loc)
        refs = set(FOOTNOTE.findall(DEFINITION.sub('', body)))
        if refs != definitions.keys():
            raise LeafRejected('dangling or unused footnote')
        return definitions

    def _validate(self, company, leaf, pack, markdown, source_files, fact_ids, version, previous, conflict, *, noop=False):
        meta, body, headings, sections = parse_markdown(markdown)
        sources, names = self._sources(company, source_files)
        facts = self._facts(company, leaf, pack, fact_ids)
        if any(fact['source_file'] not in sources for fact in facts.values()):
            raise LeafRejected('fact source missing from declared originals')
        expected_code = conflict['code'] if conflict else leaf.code
        expected_trunk = TODO_DIRECTORY if conflict else leaf.trunk
        if conflict and (meta.get('target_trunk') != leaf.trunk or meta.get('target_leaf') != leaf.code
                         or meta.get('metric') != conflict['metric']):
            raise LeafRejected('conflict target frontmatter mismatch')
        if (meta['leaf_code'] != expected_code or meta['trunk'] != expected_trunk or meta['company_id'] != company
                or meta['owner_skill'] != 'fact-leaf' or type(meta['version']) is not int or meta['version'] != version
                or not isinstance(meta['status'], str)
                or meta['status'] not in ({'conflict'} if conflict else {'active', 'archived'})
                or not isinstance(meta['sources'], list) or any(not isinstance(s, str) for s in meta['sources'])
                or sorted(set(meta['sources'])) != sources or type(meta['source_file_count']) is not int
                or meta['source_file_count'] != len(set(meta['sources']))):
            raise LeafRejected('frontmatter mismatch')
        if not isinstance(meta['title'], str) or not meta['title'].strip():
            raise LeafRejected('invalid frontmatter title')
        for field in ('latest_period', 'created_at', 'updated_at'):
            allowed = (str, date, int) if field == 'latest_period' else (str, date)
            if isinstance(meta[field], bool) or not isinstance(meta[field], allowed) or not str(meta[field]).strip():
                raise LeafRejected('invalid frontmatter text')
        definitions = self._footnotes(body, sources, names)
        if not conflict:
            expected = ['概览', '正文', '数据明细', '编辑历史']
            if leaf.shape == 'n' and '数据明细' not in headings:
                expected.remove('数据明细')
            if headings != expected:
                raise LeafRejected('required sections missing or out of order')
            for paragraph in re.split(r'\n\s*\n', sections['正文']):
                paragraph = DEFINITION.sub('', paragraph).strip()
                if paragraph and not _ends_footnote(paragraph):
                    raise LeafRejected('paragraph lacks terminal footnote')
            if '数据明细' in sections:
                header, rows = table_rows(sections['数据明细'])
                if 'fact_id' not in header or '来源' not in header:
                    raise LeafRejected('data table missing references')
                refs = set()
                for row in rows:
                    value = row[header.index('fact_id')]
                    if not re.fullmatch(r'F?[1-9][0-9]*', value):
                        raise LeafRejected('invalid table fact_id')
                    ref = int(value.removeprefix('F'))
                    if ref not in facts or not re.fullmatch(r'(?:\[\^[^\]\s]+\])+', row[header.index('来源')]):
                        raise LeafRejected('undeclared table fact or row source')
                    refs.add(ref)
                    # IDs/footnote coordinates are syntax, not factual numbers.
                    cells = [cell for i, cell in enumerate(row) if i not in (header.index('fact_id'), header.index('来源'))]
                    for cell in cells:
                        for token in numeric_tokens(prose(cell)):
                            literal = token['text'].strip()
                            # A literal unit lives in its own column in the table.
                            if '数值' in header and cell == row[header.index('数值')] and '单位' in header:
                                literal += row[header.index('单位')]
                            if numbers.validate(literal + '{f:' + str(ref) + '}', {str(ref): facts[ref]}, kind='f')[1]:
                                raise LeafRejected('data number not in referenced fact')
                if refs != facts.keys():
                    raise LeafRejected('declared and table fact ids differ')
            old_rows = history_rows(parse_markdown(previous)[3]['编辑历史']) if previous else set()
            new_rows = history_rows(sections['编辑历史'])
            labels = [row[0].removeprefix('v') for row in new_rows]
            if len(labels) != version or set(labels) != {str(v) for v in range(1, version + 1)}:
                raise LeafRejected('invalid history versions')
            added = new_rows - old_rows
            if not noop and (not old_rows <= new_rows or len(added) != 1
                    or next(iter(added))[0] not in {str(version), f'v{version}'}):
                raise LeafRejected('editing history rewritten or version missing')
            texts = [sections['正文']]
        else:
            # History timestamps/version IDs are audit metadata, not factual
            # assertions. Conflict prose still uses the ordinary numeric guard.
            texts = [text for name, text in sections.items() if name != '编辑历史']
            if not sections:
                texts = [body]
        for text in texts:
            # Only a paragraph's own footnotes may supply its quote numbers.
            for paragraph in re.split(r'\n\s*\n', DEFINITION.sub('', text)):
                paragraph = paragraph.strip()
                if conflict and paragraph.startswith('|'):
                    self._conflict_table(paragraph, facts, definitions)
                    continue
                if conflict and paragraph and not paragraph.startswith('|') and not _ends_footnote(paragraph):
                    raise LeafRejected('conflict paragraph lacks terminal footnote')
                quotes = [self.quote_resolver.resolve(file_hash=definitions[k][0], loc=definitions[k][1])
                          for k in FOOTNOTE.findall(paragraph)]
                check_union(paragraph, facts, [q for q in quotes if q is not None])
        return meta, sources, facts

    def _conflict_table(self, text, facts, definitions):
        header, rows = table_rows(text)
        for row in rows:
            keys = FOOTNOTE.findall(' '.join(row))
            if not keys:
                raise LeafRejected('conflict table row lacks source')
            row_facts = facts
            if 'fact_id' in header:
                # IDs/ranges are reference syntax, not factual quantities.
                match = re.fullmatch(r'F?([1-9][0-9]*)(?:[–-]F?([1-9][0-9]*))?', row[header.index('fact_id')])
                if not match:
                    raise LeafRejected('invalid conflict table fact reference')
                start, end = int(match[1]), int(match[2] or match[1])
                if end < start or end - start + 1 > len(facts):
                    raise LeafRejected('invalid conflict table fact range')
                refs = range(start, end + 1)
                if any(ref not in facts for ref in refs):
                    raise LeafRejected('undeclared conflict table fact')
                row_facts = {ref: facts[ref] for ref in refs}
            quotes = [self.quote_resolver.resolve(file_hash=definitions[k][0], loc=definitions[k][1]) for k in keys]
            for name, cell in zip(header, row):
                if name == 'fact_id':
                    continue
                value = cell + (row[header.index('单位')] if name == '数值' and '单位' in header else '')
                check_union(value, row_facts, [q for q in quotes if q is not None])

    def publish_leaf(self, *, company_id, leaf_code, markdown, source_files, fact_ids,
                     attribution_id=None, change_note=None, thread_id=None, job_id=None):
        return self._publish(company_id, leaf_code, markdown, source_files, fact_ids,
                             attribution_id, change_note, conflict=None, job_id=job_id)

    def publish_conflict(self, *, company_id, leaf_code, target_trunk, target_leaf,
                         metric, job_id, markdown, source_files, fact_ids,
                         attribution_id=None, change_note=None, thread_id=None):
        enabled()
        self._require_writable()
        try:
            paths.identifier(target_trunk)
            paths.identifier(metric)
            paths.identifier(job_id)
            paths._segment(leaf_code)
            prefix = f'conflict-{target_trunk}-{metric}-'
            seq = leaf_code.removeprefix(prefix)
            if not leaf_code.startswith(prefix) or not seq.isascii() or not seq.isdigit() or int(seq) < 1:
                raise ValueError('invalid independent conflict code')
            uri = paths.conflict_uri(company_id, f'conflict-{target_trunk}-{metric}-{job_id}.md')
        except (ValueError, AttributeError, TypeError) as exc:
            raise LeafRejected('invalid conflict address') from exc
        target, _ = self._leaf(company_id, target_leaf)
        if target.trunk != target_trunk:
            raise LeafRejected('conflict target trunk mismatch')
        return self._publish(company_id, leaf_code, markdown, source_files, fact_ids,
                             attribution_id, change_note,
                             conflict={'uri': uri, 'code': leaf_code, 'target_leaf': target_leaf, 'metric': metric})

    def _publish(self, company, code, markdown, source_files, fact_ids, attribution, note, conflict, job_id=None):
        enabled()
        self._require_writable()
        leaf, pack = self._leaf(company, conflict['target_leaf'] if conflict else code)
        key = code
        sha = hashlib.sha256(markdown.encode()).hexdigest() if isinstance(markdown, str) else ''
        with projection_lock(self.root, company):
            self.db.execute('BEGIN IMMEDIATE')
            published = False
            previous = None
            try:
                indexed = self.db.execute('SELECT uri FROM facts_tree_leaf_index WHERE company_id=? AND leaf_code=?', (company, key)).fetchone()
                try:
                    uri = indexed['uri'] if indexed else (conflict['uri'] if conflict else pack_leaf_uri(company, pack, leaf))
                except ValueError as exc:
                    raise LeafRejected('invalid leaf filename') from exc
                if indexed and conflict and uri != conflict['uri']:
                    raise LeafRejected('conflict job address differs from stable URI')
                previous = read_optional(self.memory, uri)
                last = self.db.execute('SELECT version,content_sha FROM leaf_revisions WHERE company_id=? AND leaf_code=? ORDER BY version DESC LIMIT 1', (company, key)).fetchone()
                same = last is not None and last['content_sha'] == sha
                version = last['version'] if same else (last['version'] + 1 if last else 1)
                if last and (previous is None or hashlib.sha256(previous.encode()).hexdigest() != last['content_sha']):
                    raise LeafRejected('previous projection differs from revision')
                # Revoked facts/sources must not bypass guards on no-op requests.
                _, sources, facts = self._validate(company, leaf, pack, markdown, source_files, fact_ids, version,
                                                   previous, conflict, noop=same)
                if same:
                    if previous != markdown:
                        raise LeafRejected('projection differs from revision')
                    self.db.rollback()
                    return {'uri': uri, 'version': version, 'changed': False}
                if indexed is None:
                    try:
                        self.db.execute('INSERT INTO facts_tree_leaf_index VALUES(?,?,?)', (company, key, uri))
                    except sqlite3.IntegrityError as exc:
                        raise LeafRejected('URI already belongs to a different leaf') from exc
                self.db.execute('''INSERT INTO leaf_revisions(company_id,leaf_code,version,content_sha,source_files,fact_ids,
                    owner_skill,attribution_id,change_note,created_at) VALUES(?,?,?,?,?,?, ?,?,?,?)''',
                    (company, key, version, sha, json.dumps(sorted(set(source_files))), json.dumps(sorted(set(fact_ids))),
                     'fact-leaf', attribution, note, datetime.now(timezone.utc).isoformat()))
                if conflict is None:
                    for ref, fact in facts.items():
                        self.db.execute("""INSERT INTO fact_leaf_links
                            (company_id,fact_id,trunk,leaf_code,source_file,job_id,created_at)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,fact_id,leaf_code)
                            DO UPDATE SET trunk=excluded.trunk,source_file=excluded.source_file,
                                          job_id=excluded.job_id""",
                            (company, ref, fact['module_key'], code, fact['source_file'], job_id,
                             datetime.now(timezone.utc).isoformat()))
                self._publish_documents(company, {uri: markdown}, metadata={'company_id': company, 'owner_skill': 'fact-leaf'})
                published = True
                self.db.commit()
                return {'uri': uri, 'version': version, 'changed': True}
            except Exception:
                self.db.rollback()
                if published and not self._joins_memory():
                    publish_pair(self.memory, {uri: previous}, metadata={'company_id': company})
                raise
