"""Coordinate-only access to explicitly granted parsed documents."""
from copy import deepcopy

from ..document_read.dossier import Dossier


class DossierQuoteResolver:
    def __init__(self, documents):
        self._documents = deepcopy(dict(documents))

    def resolve(self, *, file_hash: str, loc: str) -> str | None:
        document = self._documents.get(file_hash)
        if document is None:
            return None
        try:
            # Dossier.read never accesses objects_root or original assets.
            return Dossier(document, file_hash=file_hash, objects_root='.').read(loc)
        except (KeyError, ValueError, TypeError, IndexError):
            return None
