"""Read-only structural QC for the opt-in reader, executed only in bwrap.

Independently reads OOXML cells/assets and checks their exact projected positions.
No business interpretation, model output, credentials or networking.
"""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def text(node):
    return ''.join((n.text or '') if n.tag == W+'t' else '\t' if n.tag == W+'tab'
                   else '\n' if n.tag in (W+'br', W+'cr') else '' for n in node.iter())


def escape(value):
    return value.replace('\\','\\\\').replace('|','\\|').replace('\n','<br>').replace('\r','')


def verify(evidence_root=Path('/evidence')):
    document = json.loads((evidence_root/'document.json').read_text())
    lines = document['markdown'].splitlines()
    issues=[]
    def issue(loc, problem):
        if len(issues)<128:
            issues.append({'source_loc':loc, 'problem':problem, 'expected':'保持原件各格内容、列位置与对象链接'})
    with ZipFile(evidence_root/'original.docx') as archive:
        body=ET.fromstring(archive.read('word/document.xml')).find(W+'body')
        tables=list(body.iter(W+'tbl'))
        records=[e for e in document['document_map']['elements'] if e['kind']=='table']
        if len(tables)!=len(records): issue('docx:body','表格数量不一致')
        for table,record in zip(tables,records):
            grid=[]; width=len(table.find(W+'tblGrid')) if table.find(W+'tblGrid') is not None else 0
            for row in table.findall(W+'tr'):
                values=[]
                before=row.find(W+'trPr/'+W+'gridBefore')
                if before is not None: values.extend(['']*int(before.get(W+'val','0')))
                for cell in row.findall(W+'tc'):
                    span=cell.find(W+'tcPr/'+W+'gridSpan')
                    count=int(span.get(W+'val','1')) if span is not None else 1
                    vertical=cell.find(W+'tcPr/'+W+'vMerge')
                    content='\n'.join(text(p) for p in cell.findall(W+'p'))
                    if vertical is not None and vertical.get(W+'val','continue')!='restart':
                        content='↑（合并自上）'+(' '+content if content else '')
                    if count>1: content+=f' （colspan={count}）'
                    values.append(escape(content));values.extend(['←（合并自左）']*(count-1))
                width=max(width,len(values));grid.append(values)
            expected=['| '+' | '.join(row+['']*(width-len(row)))+' |' for row in grid]
            actual=lines[record['start_line']-1:record['start_line']+len(grid)]
            if not expected or len(actual)<2 or actual[1]!='|'+'---|'*width:
                issue(record['source_loc'],'表头分隔或列数错误');continue
            actual.pop(1)
            if actual!=expected: issue(record['source_loc'],'原件逐格内容或列位置不一致')
        members={n for n in archive.namelist() if n.startswith(('word/media/','word/embeddings/')) and not n.endswith('/')}
        assets=document['assets']
        if members!={a['archive_member'] for a in assets} or len(members)!=len(assets):
            issue('docx:body','对象清单数量不一致')
        for asset in assets:
            if asset['archive_member'] not in members:
                issue(asset['source_loc'],'对象不在原件中');continue
            if hashlib.sha256(archive.read(asset['archive_member'])).hexdigest()!=asset['sha256']:
                issue(asset['source_loc'],'对象哈希不一致')
            if document['markdown'].count(']('+asset['uri']+')')!=1:
                issue(asset['source_loc'],'对象原位链接不唯一')
            located=[e for e in document['document_map']['elements'] if e.get('uri')==asset['uri']]
            if len(located)!=1 or ']('+asset['uri']+')' not in '\n'.join(lines[located[0]['start_line']-1:located[0]['end_line']]):
                issue(asset['source_loc'],'对象链接不在地图指定位置')
    return {'status':'rework' if issues else 'pass','issues':issues}


if __name__ == '__main__':
    print(json.dumps(verify(),ensure_ascii=False))
