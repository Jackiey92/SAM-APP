"""Employee artifact shape only; no content decisions or repairs."""
from .leaf_store import LeafRejected


def parse_employee_leaf(raw: dict) -> dict:
    if isinstance(raw, dict) and raw.get('empty') is True:
        if not isinstance(raw.get('leaf_code'), str) or not isinstance(raw.get('note'), str):
            raise LeafRejected('invalid empty leaf fields')
        if raw.get('markdown', '') != '' or raw.get('source_files', []) != [] or raw.get('fact_ids', []) != []:
            raise LeafRejected('empty leaf carries content')
        return dict(leaf_code=raw['leaf_code'], empty=True, note=raw['note'])
    if isinstance(raw, dict) and 'empty' in raw and type(raw['empty']) is not bool:
        raise LeafRejected('invalid empty type')
    fields = ('leaf_code', 'markdown', 'source_files', 'fact_ids', 'note')
    if not isinstance(raw, dict) or any(key not in raw for key in fields):
        raise LeafRejected('missing employee leaf fields')
    for key in ('leaf_code', 'markdown', 'note'):
        if not isinstance(raw[key], str):
            raise LeafRejected(f'invalid {key} type')
    if not raw['markdown'].strip():
        raise LeafRejected('empty markdown')
    for key, item_type in (('source_files', str), ('fact_ids', int)):
        if not isinstance(raw[key], list) or any(type(item) is not item_type for item in raw[key]):
            raise LeafRejected(f'invalid {key} type')
    return {key: list(raw[key]) if isinstance(raw[key], list) else raw[key] for key in fields}
