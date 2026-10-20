"""Local-only snapshot assembly using existing scoped SAM read services."""
from __future__ import annotations

import time

from . import settings
from .business_overview import BusinessOverviewService
from .module_taxonomy import module_definitions
from .classifier import ClassificationService
from .cloud_snapshot import SNAPSHOT_VERSION
from .memory.user_memory.l2_conversation import ConversationStore
from .dashboard import DashboardPreferenceStore, assemble_dashboard
from .db.database import connect
from .entities import EntityRosterService
from .memory.company_facts import ConsolidationService
from .knowledge import KnowledgeService
from .providers import memory_provider
from .runtime_config import RuntimeConfig
from .thread_manager import ThreadManager


def build_home_snapshot(*, company_id: str) -> dict:
    """Read the host-selected company without creating jobs or starting workers.

    Business projections are supplied unchanged by the current services. Only
    container assembly and file-count aggregation occur here; no classifier,
    model call, extraction, consolidation, promotion or memory write is made.
    The local ledger may change during reads; push again for a fresher snapshot.
    """
    config = RuntimeConfig.from_env()
    if config.cloud_readonly:
        raise RuntimeError('homepage export requires local mode, not SAM_CLOUD_READONLY')
    memory = memory_provider(config)
    facts = ConsolidationService(config.main_db).list_facts(company_id=company_id)
    todos = ConsolidationService(config.main_db).list_todos(company_id=company_id, status=None)
    files = ClassificationService(config.main_db).list_current(company_id=company_id)
    module_counts = {}
    for item in files:
        module = item.get('module')
        if module:
            module_counts[str(module)] = module_counts.get(str(module), 0) + 1
    with connect(config.main_db) as conn:
        labels = {row['code']: row['name'] for row in conn.execute('SELECT code,name FROM modules')}
        jobs = [dict(row) for row in conn.execute(
            'SELECT * FROM parse_jobs WHERE company_id=? ORDER BY id DESC', (company_id,))]
    overview = BusinessOverviewService(config.main_db, memory).overview(company_id=company_id)
    hidden = DashboardPreferenceStore(config.main_db).get(company_id=company_id)
    conversation_store = ConversationStore(memory)
    conversations = [{**thread, 'turns': conversation_store.read_range(company_id, thread['thread_id'])}
                     for thread in ThreadManager(memory).list(company_id)]
    visible_modules = module_definitions(include_hidden=False)
    if settings.SAM_FACTS_TREE:
        from .facts_tree.visibility import active_trunks
        active = set(active_trunks(db_path=config.main_db, memory=memory, company_id=company_id))
        visible_modules = [m for m in visible_modules if m['code'] in active]
    payload = {
        'page_context': {'facts': facts, 'todos': todos, 'files': files,
                         'module_counts': module_counts, 'module_labels': labels,
                         'visible_modules': visible_modules},
        'knowledge': KnowledgeService(config.main_db, memory, objects_path=config.objects_dir).read(company_id=company_id),
        'overview': overview,
        'dashboard': assemble_dashboard(overview=overview, facts=facts, hidden=hidden),
        'dashboard_preferences': hidden,
        'entities': EntityRosterService(config.main_db).list(company_id=company_id),
        'jobs': jobs, 'conversations': conversations,
    }
    return {'version': SNAPSHOT_VERSION, 'company_id': company_id, 'pushed_at': time.time(),
            'payload': payload}


def build_facts_tree_snapshot(*, company_id: str) -> dict:
    """Dedicated FactsTree-only envelope for the /ingest/facts-tree channel.

    Local mode and the feature gate only: the section comes from the
    read-only TreeReadService, so a consistency failure aborts the export
    rather than pushing unverified content. The payload holds exactly the
    facts_tree section; unbound companies keep the existing unbound state.
    A ready section is stamped with the placement contract marker (DEC-020
    option A): the identity/placement ledgers were fully verified locally and
    never leave the local database.
    """
    if not settings.SAM_FACTS_TREE:
        raise RuntimeError('facts-tree export requires SAM_FACTS_TREE')
    config = RuntimeConfig.from_env()
    if config.cloud_readonly:
        raise RuntimeError('facts-tree export requires local mode, not SAM_CLOUD_READONLY')
    memory = memory_provider(config)
    # FactsTree documents and navigation metadata only; nothing else is read.
    from .facts_tree.tree_read import TreeReadService, attach_placement_validation
    with TreeReadService(config.main_db, memory, company_id=company_id) as tree_reader:
        facts_tree = tree_reader.snapshot(company_id)
    if facts_tree['state'] == 'ready':
        attach_placement_validation(facts_tree)
    return {'version': SNAPSHOT_VERSION, 'company_id': company_id, 'pushed_at': time.time(),
            'payload': {'facts_tree': facts_tree}}
