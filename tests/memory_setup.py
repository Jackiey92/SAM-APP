"""Explicit temporary-store setup for tests; production constructor is read-only."""
from app.db.database import init_db
from app.memory.provider import LocalVectorMemoryProvider


def migrated_memory(database, **kwargs):
    init_db(database, embedding=kwargs.get('embedding'))
    return LocalVectorMemoryProvider(database, **kwargs)
