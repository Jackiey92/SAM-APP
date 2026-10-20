"""Deterministic OOXML-to-Markdown projection; no content classification.

Coordinates refer to physical XML body children and table grid cells. The
original remains authoritative. Unsupported layout/assets are explicit warnings.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from urllib.parse import quote

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS = {'w': W}


def tag(name):
    return '{' + W + '}' + name


def text(node):
    parts = []
    for item in node.iter():
        if item.tag == tag('t'):
            parts.append(item.text or '')
        elif item.tag == tag('tab'):
            parts.append('\t')
        elif item.tag in {tag('br'), tag('cr')}:
            parts.append('\n')
    return ''.join(parts)


def escape(value):
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', '<br>').replace('\r', '')


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def project_docx(source: Path, file_hash: str, objects_root: Path) -> dict:
    """Return md, structural map, assets and independently counted self-check.

    Only reads the source; writes extracted bytes below objects_root/file_hash.
    No conversion executable is assumed: unsupported images retain original
    links and an explicit visual-review warning rather than fake snapshots.
    """
    if len(file_hash) != 64 or any(c not in '0123456789abcdef' for c in file_hash):
        raise ValueError('invalid file hash')
    if hashlib.sha256(source.read_bytes()).hexdigest() != file_hash:
        raise ValueError('source hash mismatch')
    lines, elements, assets, hyperlinks, warnings = [], [], [], [], []
    tables, headings, visible = [], [], []
    with ZipFile(source) as archive:
        root = ET.fromstring(archive.read('word/document.xml'))
        body = root.find('w:body', NS)
        if body is None:
            raise ValueError('Word document has no body')
        relationships = {}
        if 'word/_rels/document.xml.rels' in archive.namelist():
            for rel in ET.fromstring(archive.read('word/_rels/document.xml.rels')):
                relationships[rel.get('Id')] = rel.attrib
        styles = {}
        if 'word/styles.xml' in archive.namelist():
            for style in ET.fromstring(archive.read('word/styles.xml')).findall('w:style', NS):
                outline = style.find('w:pPr/w:outlineLvl', NS)
                parent = style.find('w:basedOn', NS)
                styles[style.get(tag('styleId'))] = (
                    int(outline.get(tag('val'))) if outline is not None else None,
                    parent.get(tag('val')) if parent is not None else None,
                )

        def level(node):
            outline = node.find('w:pPr/w:outlineLvl', NS)
            if outline is not None:
                value = int(outline.get(tag('val')))
                return value + 1 if value < 9 else None
            style = node.find('w:pPr/w:pStyle', NS)
            key = style.get(tag('val')) if style is not None else None
            seen = set()
            while key in styles and key not in seen:
                seen.add(key)
                value, key = styles[key]
                if value is not None:
                    return value + 1 if value < 9 else None
            return None

        def emit(value, loc, kind, **metadata):
            start = len(lines) + 1
            lines.extend(value.splitlines() + [''])
            record = {'kind': kind, 'source_loc': loc, 'start_line': start,
                      'end_line': len(lines) - 1, **metadata}
            elements.append(record)
            return record

        def asset(node, loc):
            # DrawingML, VML and OLE expose relationship IDs independently of
            # business meaning. A single referenced ZIP member is one asset;
            # multiple placements are retained separately for reconciliation.
            refs = []
            for child in node.iter():
                for attr in ('embed', 'id', 'link'):
                    rid = child.get('{' + R + '}' + attr)
                    rel = relationships.get(rid, {})
                    if rel.get('TargetMode') == 'External':
                        continue
                    target = posixpath.normpath(posixpath.join('word', rel.get('Target', '')))
                    if target.startswith(('word/media/', 'word/embeddings/')) and target in archive.namelist():
                        if target not in refs:
                            refs.append(target)
            for target in refs:
                number = len(assets) + 1
                suffix = PurePosixPath(target).suffix.lower()
                relative = PurePosixPath('media' if '/media/' in target else 'embeddings') / PurePosixPath(target).name
                payload = archive.read(target)
                destination = objects_root / file_hash / str(relative)
                atomic_bytes(destination, payload)
                uri = f'sam://object/{file_hash}/{relative}'
                position = loc + f'/object/{number}'
                viewable = suffix in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
                notice = '' if viewable else '；需回看原件/视觉处理（无可用快照，页码未知）'
                original = f'sam://original/{file_hash}'
                emit(f'![图/对象{number}（{position}）]({uri}) ^[loc {position}]' +
                     (f'\n[原件回链：{notice[1:]}]({original})' if notice else ''), position, 'object', uri=uri)
                assets.append({'uri': uri, 'original_uri': original, 'source_loc': position,
                               'archive_member': target, 'local_path': str(destination),
                               'sha256': hashlib.sha256(payload).hexdigest(), 'size_bytes': len(payload),
                               'view_status': 'direct' if viewable else 'needs_visual_review'})
                if notice:
                    warnings.append(f'{position}: {notice[1:]}')

        def paragraph(node, loc):
            # Preserve run order, including pictures between text runs.
            visible.append(text(node))
            buffer = []
            heading = level(node)
            def flush():
                value = ''.join(buffer)
                buffer.clear()
                if value.strip():
                    record = emit(('#' * min(heading, 6) + ' ' if heading else '') +
                                  value + f' ^[loc {loc}]', loc, 'heading' if heading else 'paragraph',
                                  **({'level': heading} if heading else {}))
                    if heading:
                        headings.append(record)
            def visit(child):
                if child.tag == tag('pPr'):
                    return
                if child.tag in {tag('drawing'), tag('pict'), tag('object')}:
                    flush()
                    asset(child, loc)
                    # Object containers may include textual fallbacks.
                    value = text(child)
                    if value:
                        buffer.append(value)
                    return
                if child.tag == tag('hyperlink'):
                    rel = relationships.get(child.get('{' + R + '}id'), {})
                    value = text(child)
                    if rel.get('Target'):
                        value = '[' + value.replace(']', '\\]') + '](' + quote(rel['Target'], safe='/:#?=&%') + ')'
                    buffer.append(value)
                    hyperlinks.append({'source_loc': loc, 'text': text(child), 'target': rel.get('Target'),
                                       'anchor': child.get(tag('anchor'))})
                    return
                if child.tag == tag('t'):
                    buffer.append(child.text or '')
                elif child.tag == tag('tab'):
                    buffer.append('\t')
                elif child.tag in {tag('br'), tag('cr')}:
                    buffer.append('\n')
                else:
                    for descendant in child:
                        visit(descendant)
            for child in node:
                visit(child)
            flush()

        def table(node, loc):
            rows = node.findall('w:tr', NS)
            width = len(node.findall('w:tblGrid/w:gridCol', NS))
            declared_width = width
            grid, merges, nested = [], [], []
            for ri, row in enumerate(rows):
                values = []
                before = row.find('w:trPr/w:gridBefore', NS)
                if before is not None:
                    values.extend([''] * int(before.get(tag('val'), '0')))
                for ci, cell in enumerate(row.findall('w:tc', NS)):
                    column = len(values)
                    span_node = cell.find('w:tcPr/w:gridSpan', NS)
                    span = int(span_node.get(tag('val'), '1')) if span_node is not None else 1
                    vertical = cell.find('w:tcPr/w:vMerge', NS)
                    continuation = vertical is not None and vertical.get(tag('val'), 'continue') != 'restart'
                    parts = []
                    for ni, child in enumerate(cell):
                        cell_loc = loc + f'/row/{ri}/cell/{column}/{ni}'
                        if child.tag == tag('p'):
                            value = text(child)
                            visible.append(value)
                            parts.append(value)
                            # Emit objects after the containing table;
                            # their cell coordinates retain exact placement.
                            nested.append(('assets', child, cell_loc))
                            if any(c.get('{' + R + '}' + a) in relationships
                                   and relationships[c.get('{' + R + '}' + a)].get('Target', '').startswith(('media/', 'embeddings/'))
                                   for c in child.iter() for a in ('embed', 'id', 'link')):
                                warnings.append(cell_loc + ': 表内对象以格坐标回链；需视觉核验版式')
                        elif child.tag == tag('tbl'):
                            nested.append(('table', child, cell_loc + '/table'))
                            parts.append(f'[嵌套表：{cell_loc}/table]')
                    value = '\n'.join(parts)
                    if continuation:
                        value = '↑（合并自上）' + (f' {value}' if value else '')
                    if span > 1:
                        value += f' （colspan={span}）'
                    if span > 1 or vertical is not None and not continuation:
                        merges.append({'row': ri, 'column': column, 'colspan': span,
                                       'vertical': vertical is not None})
                    values.append(escape(value))
                    values.extend(['←（合并自左）'] * (span - 1))
                if declared_width and len(values) > declared_width:
                    warnings.append(loc + f'/row/{ri}: 单元格跨度超过原件列网格')
                width = max(width, len(values))
                grid.append(values)
            grid = [r + [''] * (width - len(r)) for r in grid]
            emit(f'**表{len(tables) + 1}** ^[loc {loc}]', loc, 'table_title')
            start = len(lines) + 1
            if grid:
                lines.append('| ' + ' | '.join(grid[0]) + ' |')
                lines.append('|' + '---|' * width)
                for ri, values in enumerate(grid[1:], 1):
                    lines.append('| ' + ' | '.join(values) + ' |')
            lines.extend(['', f'原件 {len(rows)} 行 × {width} 列、合并 {len(merges)} 处。 ^[loc {loc}]', ''])
            record = {'source_loc': loc, 'rows': len(rows), 'columns': width, 'merges': merges,
                      'start_line': start, 'end_line': len(lines) - 1}
            tables.append(record)
            elements.append({'kind': 'table', **record})
            for kind, child, child_loc in nested:
                if kind == 'table':
                    table(child, child_loc)
                else:
                    asset(child, child_loc)

        def walk(container, path=()):
            for index, child in enumerate(container):
                order = (*path, index)
                loc = 'docx:body/' + '/'.join(map(str, order))
                if child.tag == tag('p'):
                    paragraph(child, loc + '/paragraph')
                elif child.tag == tag('tbl'):
                    table(child, loc + '/table')
                elif child.tag not in {tag('sectPr'), tag('tcPr'), tag('tblPr'), tag('tblGrid')}:
                    walk(child, order)
        walk(body)
        source_text = ''.join(n.text or '' for n in body.iter(tag('t')))
        projected_text = ''.join(visible)
        source_chars = Counter(c for c in source_text if not c.isspace())
        rendered_chars = Counter(c for c in projected_text if not c.isspace())
        total = sum(source_chars.values())
        coverage = 100.0 if not total else 100 * sum((source_chars & rendered_chars).values()) / total
        source_tables = list(body.iter(tag('tbl')))
        archive_assets = [n for n in archive.namelist() if n.startswith(('word/media/', 'word/embeddings/')) and not n.endswith('/')]
        linked_assets = {a['archive_member'] for a in assets}
        missing = sorted(set(archive_assets) - linked_assets)
        if missing:
            warnings.append('未能定位的原件对象：' + ', '.join(missing))
        source_sequence = ''.join(c for c in source_text if not c.isspace())
        rendered_sequence = ''.join(c for c in projected_text if not c.isspace())
        grams = {source_sequence[i:i + 8] for i in range(max(0, len(source_sequence) - 7))}
        hits = sum(g in rendered_sequence for g in grams)
        sequence_coverage = 100 * hits / len(grams) if grams else coverage
        if sequence_coverage < 99:
            warnings.append(f'原文连续八字片段覆盖率低于 99%：{sequence_coverage:.2f}%')
        if coverage < 99:
            warnings.append(f'可见文字覆盖率低于 99%：{coverage:.2f}%')
        if len(source_tables) != len(tables):
            warnings.append('原件表格数与正文不一致')
        if len(archive_assets) != len(assets):
            warnings.append('对象数/正文链接数不一致（可能存在复用或未定位对象）')
        # Headers/footers/textboxes need explicit inspection, never a silent
        # "100% document coverage" claim based only on body text.
        other_parts = [n for n in archive.namelist() if n.startswith(('word/header', 'word/footer')) and n.endswith('.xml')]
        if other_parts:
            warnings.append('覆盖率仅针对正文；页眉/页脚待原件核验：' + ', '.join(other_parts))
        check = {'status': 'warning' if warnings else 'pass', 'warnings': warnings,
                 'coverage_scope': 'document body visible text character multiset; not order validation',
                 'text_coverage_percent': round(coverage, 4),
                 'eight_character_fragment_coverage_percent': round(sequence_coverage, 4), 'source_character_count': total,
                 'source_table_count': len(source_tables), 'rendered_table_count': len(tables),
                 'table_dimensions': [{'source_rows': len(t.findall('w:tr', NS)),
                                       'source_grid_columns': len(t.findall('w:tblGrid/w:gridCol', NS)),
                                       'rendered_rows': record['rows'], 'rendered_columns': record['columns'],
                                       'source_loc': record['source_loc']}
                                      for t, record in zip(source_tables, tables)],
                 'source_object_count': len(archive_assets), 'extracted_object_count': len(linked_assets),
                 'markdown_object_link_count': len(assets), 'asset_manifest_count': len(assets),
                 'unlocated_objects': missing}
    return {'version': 1, 'markdown': '\n'.join(lines).rstrip() + '\n',
            'document_map': {'elements': elements, 'headings': headings, 'tables': tables,
                             'objects': [{'uri': a['uri'], 'source_loc': a['source_loc']} for a in assets],
                             'line_count': len(('\n'.join(lines).rstrip() + '\n').splitlines()), 'character_count': len(projected_text)},
            'assets': assets, 'hyperlinks': hyperlinks, 'self_check': check}
