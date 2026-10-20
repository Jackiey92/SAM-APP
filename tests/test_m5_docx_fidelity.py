"""Stage A pure-format preview contracts; never touch a product database."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

SCRIPT = Path('skills/document-ingest/scripts/docx_fidelity.py')
spec = importlib.util.spec_from_file_location('docx_fidelity', SCRIPT)
fidelity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fidelity)
W = fidelity.W
R = fidelity.R


def document(tmp_path, body, *, media=None, relationships='', styles=''):
    path = tmp_path / 'acc-format.docx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:a="urn:drawing"><w:body>{body}</w:body></w:document>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships>' + relationships + '</Relationships>')
        if styles:
            archive.writestr('word/styles.xml', f'<w:styles xmlns:w="{W}">{styles}</w:styles>')
        for name, data in (media or {}).items():
            archive.writestr(name, data)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest


def p(value):
    return '<w:p><w:r><w:t>' + value + '</w:t></w:r></w:p>'


def cell(value, props=''):
    return '<w:tc><w:tcPr>' + props + '</w:tcPr>' + p(value) + '</w:tc>'


def render(tmp_path, body, **kw):
    source, digest = document(tmp_path, body, **kw)
    before = source.read_bytes()
    result = fidelity.project_docx(source, digest, tmp_path / 'output' / 'objects')
    assert source.read_bytes() == before
    return result


def test_paragraph_table_paragraph_order_and_true_financial_table(tmp_path):
    rows = [('项目', '2022年', '2023年', '2024年'),
            ('营业收入（万元）', '10543.93', '15099.24', '11413.05'),
            ('研发费用（万元）', '601.20', '863.19', '626.49')]
    table = '<w:tbl><w:tblGrid>' + '<w:gridCol/>' * 4 + '</w:tblGrid>' + ''.join(
        '<w:tr>' + ''.join(cell(c) for c in row) + '</w:tr>' for row in rows) + '</w:tbl>'
    result = render(tmp_path, p('开始') + table + p('结束'))
    md = result['markdown']
    assert md.index('开始') < md.index('| 项目 |') < md.index('结束')
    assert '|---|---|---|---|' in md
    assert '| 营业收入（万元） | 10543.93 | 15099.24 | 11413.05 |' in md
    assert '| 研发费用（万元） | 601.20 | 863.19 | 626.49 |' in md
    assert '原件 3 行 × 4 列、合并 0 处' in md
    assert '<!--' not in md and '\n- ' not in md and 'None' not in md
    assert result['self_check']['status'] == 'pass'
    assert result['self_check']['eight_character_fragment_coverage_percent'] == 100


def test_inherited_heading_and_run_join_not_business_inference(tmp_path):
    styles = '<w:style w:styleId="Base"><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style><w:style w:styleId="Child"><w:basedOn w:val="Base"/></w:style>'
    heading = '<w:p><w:pPr><w:pStyle w:val="Child"/></w:pPr><w:r><w:t>标题</w:t></w:r></w:p>'
    split_number = '<w:p><w:r><w:t>150</w:t></w:r><w:r><w:t>99.24</w:t></w:r></w:p>'
    result = render(tmp_path, heading + split_number, styles=styles)
    assert result['markdown'].startswith('## 标题 ^[loc docx:body/0/paragraph]')
    assert '15099.24' in result['markdown']
    assert result['document_map']['headings'][0]['level'] == 2


def test_horizontal_vertical_merge_grid_preserves_cells(tmp_path):
    body = '<w:tbl><w:tblGrid>' + '<w:gridCol/>' * 3 + '</w:tblGrid><w:tr>' + cell('A', '<w:gridSpan w:val="2"/><w:vMerge w:val="restart"/>') + cell('B') + '</w:tr><w:tr>' + cell('', '<w:gridSpan w:val="2"/><w:vMerge/>') + cell('C') + '</w:tr></w:tbl>'
    result = render(tmp_path, body)
    assert 'colspan=2' in result['markdown']
    assert '↑（合并自上）' in result['markdown']
    assert '←（合并自左）' in result['markdown']
    table = result['document_map']['tables'][0]
    assert table['columns'] == 3 and len(table['merges']) == 2  # spans on both physical rows


def test_picture_at_run_position_and_object_accounting(tmp_path):
    body = '<w:p><w:r><w:t>前</w:t></w:r><w:r><w:drawing><a:blip r:embed="r1"/></w:drawing></w:r><w:r><w:t>后</w:t></w:r></w:p>'
    result = render(tmp_path, body, media={'word/media/image1.png': b'png-object'},
                    relationships='<Relationship Id="r1" Target="media/image1.png"/>')
    md = result['markdown']
    assert md.index('前') < md.index('![图/对象1') < md.index('后')
    check = result['self_check']
    assert [check[k] for k in ('source_object_count', 'extracted_object_count', 'markdown_object_link_count', 'asset_manifest_count')] == [1, 1, 1, 1]
    assert Path(result['assets'][0]['local_path']).read_bytes() == b'png-object'


def test_unsupported_emf_retained_and_warning_not_silent_success(tmp_path):
    body = '<w:p><w:r><w:drawing><a:blip r:embed="r1"/></w:drawing></w:r></w:p>'
    result = render(tmp_path, body, media={'word/media/image1.emf': b'emf-object'},
                    relationships='<Relationship Id="r1" Target="media/image1.emf"/>')
    assert '需回看原件/视觉处理' in result['markdown']
    assert 'sam://original/' in result['markdown']
    assert result['assets'][0]['view_status'] == 'needs_visual_review'
    assert result['self_check']['status'] == 'warning'


def test_unlocated_asset_is_explicit_quality_issue(tmp_path):
    result = render(tmp_path, p('正文'), media={'word/media/unused.png': b'not-located'})
    assert result['self_check']['status'] == 'warning'
    assert result['self_check']['unlocated_objects'] == ['word/media/unused.png']
    assert result['self_check']['markdown_object_link_count'] == 0


def test_hyperlinks_inventory_and_markdown_escape(tmp_path):
    result = render(tmp_path, '<w:p><w:hyperlink r:id="r1"><w:r><w:t>参考文件</w:t></w:r></w:hyperlink></w:p>',
                    relationships='<Relationship Id="r1" Target="资料/a b.docx" TargetMode="External"/>')
    assert '[参考文件](' in result['markdown'] and 'a%20b.docx' in result['markdown']
    assert result['hyperlinks'][0]['target'] == '资料/a b.docx'
    assert result['self_check']['text_coverage_percent'] == 100


def test_hash_mismatch_cannot_write_assets(tmp_path):
    source, _ = document(tmp_path, p('text'))
    with pytest.raises(ValueError, match='hash mismatch'):
        fidelity.project_docx(source, 'a' * 64, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_preview_cli_artifacts_and_explicit_output(tmp_path):
    source, _ = document(tmp_path, p('text'))
    out = tmp_path / 'output'
    process = subprocess.run([sys.executable, str(SCRIPT.with_name('render_docx.py')), str(source), '--output-dir', str(out)], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads((out / 'self_check.json').read_text())['status'] == 'pass'
    assert (out / 'document.md').is_file() and (out / 'document_map.json').is_file()
    refused = subprocess.run([sys.executable, str(SCRIPT.with_name('render_docx.py')), str(source), '--output-dir', str(tmp_path)], capture_output=True)
    assert refused.returncode != 0


def test_picture_between_text_nodes_of_one_run(tmp_path):
    body = '<w:p><w:r><w:t>前</w:t><w:drawing><a:blip r:embed="r1"/></w:drawing><w:t>后</w:t></w:r></w:p>'
    result = render(tmp_path, body, media={'word/media/image1.png': b'image'},
                    relationships='<Relationship Id="r1" Target="media/image1.png"/>')
    md = result['markdown']
    assert md.index('前') < md.index('![图/对象1') < md.index('后')


def test_preview_cli_warning_exit_retains_original_and_inventory(tmp_path):
    source, _ = document(tmp_path, p('text'), media={'word/media/unplaced.emf': b'object'})
    before = source.read_bytes()
    out = tmp_path / 'output'
    process = subprocess.run([sys.executable, str(SCRIPT.with_name('render_docx.py')), str(source), '--output-dir', str(out)], capture_output=True, text=True)
    assert process.returncode == 2
    assert json.loads((out / 'self_check.json').read_text())['status'] == 'warning'
    assert source.read_bytes() == before


def test_map_line_ranges_count_multiline_paragraphs(tmp_path):
    body = '<w:p><w:r><w:t>前段</w:t><w:br/><w:t>第二行</w:t></w:r></w:p><w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr>' + cell('表头') + '</w:tr><w:tr>' + cell('数值') + '</w:tr></w:tbl>'
    result = render(tmp_path, body)
    lines = result['markdown'].splitlines()
    table = result['document_map']['tables'][0]
    assert lines[table['start_line'] - 1] == '| 表头 |'
    assert result['document_map']['line_count'] == len(lines)
