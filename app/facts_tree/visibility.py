"""Read-only visibility derived from validated, nonempty active leaves."""
import sqlite3

from .leaf_store import LeafPublishService
from .rollup import FactsTreeRollup
from .template_packs import load_pack
from .tree_meta import require_enabled


def active_trunks(*, db_path, memory, company_id):
    require_enabled()
    store = None
    try:
        store = LeafPublishService.open_readonly(db_path, memory, company_id=company_id)
        binding = store.binding.get(company_id)
        if binding is None:
            return []
        pack = load_pack(binding['template_pack'])
        if pack.version != binding['pack_version']:
            raise ValueError('bound pack version unavailable')
        codes = {meta['trunk'] for _, _, meta, _ in FactsTreeRollup(store)._active(company_id)}
        return [trunk['code'] for trunk in pack.trunks if trunk['code'] in codes]
    except sqlite3.OperationalError:
        # Legacy/missing ledgers have no initialized facts tree. Never migrate
        # from a read-only snapshot query. Artifact validation errors still escape.
        return []
    finally:
        if store is not None:
            store.close()


def visible_modules_for_company(*, db_path, memory, company_id):
    require_enabled()
    store = None
    try:
        store = LeafPublishService.open_readonly(db_path, memory, company_id=company_id)
        binding = store.binding.get(company_id)
        if binding is None:
            pack = load_pack()
            active = set()
        else:
            pack = load_pack(binding['template_pack'])
            if pack.version != binding['pack_version']:
                raise ValueError('bound pack version unavailable')
            active = {meta['trunk'] for _, _, meta, _ in FactsTreeRollup(store)._active(company_id)}
    except sqlite3.OperationalError:
        pack, active = load_pack(), set()
    finally:
        if store is not None:
            store.close()
    return [dict(code=trunk['code'], name=trunk['directory'], sort_order=trunk['order'],
                 hidden=trunk['code'] not in active) for trunk in pack.trunks]
