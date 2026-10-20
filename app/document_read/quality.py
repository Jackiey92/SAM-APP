"""Host-owned one-shot structural verification; the reader cannot self-pass."""
import hashlib
import json
from pathlib import Path


def verify_dossier(dossier, *, source, sandbox, company_id, task_id):
    source = Path(source)
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != dossier.file_hash:
        raise ValueError('original hash differs from dossier')
    script = (Path(__file__).resolve().parents[2] /
              'skills/document-ingest/scripts/verify_direct_docx.py').read_text()
    for asset in dossier.document['assets']:
        dossier.sam_file_get(asset['uri'])
    result = sandbox.run(role='file_processor', company_id=company_id, task_id=task_id,
        script=script, evidence={'original.docx':original,
            'document.json':json.dumps(dossier.document, ensure_ascii=False).encode()})
    if result['status'] != 'pass':
        raise ValueError('document fidelity check failed: ' + json.dumps(result, ensure_ascii=False))
    dossier.quality_fingerprint = dossier.fingerprint
    return result
