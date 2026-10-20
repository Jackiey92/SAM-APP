"""Read-only scoped content-conflict views; no arbitration or migration."""
import json
import sqlite3
from pathlib import Path


def list_md_todos(db_path, *, company_id, status='open'):
    if status not in (None, 'open', 'pending'): return []
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='md_leaf_todos'").fetchone(): return []
        rows = db.execute("SELECT * FROM md_leaf_todos WHERE company_id=? AND status='pending' ORDER BY operation_id,ordinal", (company_id,)).fetchall()
        return [dict(id=f'md:{r["operation_id"]}:{r["ordinal"]}', kind='md_content_conflict',
            company_id=company_id, reason='md_conflict', status='open', review_status='pending',
            operation_id=r['operation_id'], ordinal=r['ordinal'], leaf_code=r['leaf_code'],
            base_version=r['base_version'], details=json.loads(r['payload'])) for r in rows]


def safe_md_view(item, *, company_id):
    """Untrusted cloud snapshot can only supply escaped, typed display fields."""
    if (not isinstance(item,dict) or item.get('kind')!='md_content_conflict'
            or item.get('company_id')!=company_id): return None
    details=item.get('details')
    if not isinstance(details,dict): return None
    sides=[]
    for side in (details.get('old'), details.get('new')):
        if not isinstance(side,dict): return None
        evidence=side.get('evidence')
        if not isinstance(evidence,list): return None
        sources=[]
        for e in evidence:
            if not isinstance(e,dict): return None
            if any(not isinstance(e.get(k),str) for k in ('original_name','source_loc','file_hash')): return None
            sources.append({**{k:e[k] for k in ('original_name','source_loc','file_hash')},
                'start_line':e.get('start_line') if type(e.get('start_line')) is int else None,
                'end_line':e.get('end_line') if type(e.get('end_line')) is int else None,
                'fact_ids':[v for v in e.get('fact_ids',[]) if type(v) is int] if isinstance(e.get('fact_ids',[]),list) else []})
        sides.append(dict(text=side.get('text') if isinstance(side.get('text'),str) else '',
            markdown=side.get('markdown') if isinstance(side.get('markdown'),str) else '',evidence=sources))
    def string(mapping,key):return mapping.get(key) if isinstance(mapping.get(key),str) else ''
    return dict(kind='md_content_conflict',company_id=company_id,id=string(item,'id'),
        operation_id=string(item,'operation_id'),ordinal=item.get('ordinal') if type(item.get('ordinal')) is int else '',
        leaf_code=string(item,'leaf_code'),base_version=item.get('base_version') if type(item.get('base_version')) is int else '',
        details=dict(old=sides[0],new=sides[1],position=string(details,'position'),reason=string(details,'reason'),job_id=string(details,'job_id')))
