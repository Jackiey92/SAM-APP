"""Task-scoped coordinate and asset access. No content decisions."""
from hashlib import sha256
from pathlib import Path
import json


class Dossier:
    def __init__(self, document, *, file_hash, objects_root):
        self.document = document
        self.file_hash = file_hash
        self.objects_root = Path(objects_root).resolve()
        self.lines = document['markdown'].splitlines()
        self.elements = document['document_map']['elements']
        for item in self.elements:
            if not (type(item['start_line']) is int and type(item['end_line']) is int
                    and 1 <= item['start_line'] <= item['end_line'] <= len(self.lines)):
                raise ValueError('invalid document coordinates')

    @classmethod
    def from_quality(cls, document, *, file_hash, objects_root, company_id, job_id, quality):
        dossier = cls(document, file_hash=file_hash, objects_root=objects_root)
        expected = dict(company_id=company_id, job_id=job_id, file_hash=file_hash,
                        document_fingerprint=dossier.fingerprint)
        if not isinstance(quality, dict) or quality.get('status') != 'pass' or quality.get('evidence_binding') != expected:
            raise ValueError('quality proof differs from mapped document/job/company/source')
        dossier.quality_fingerprint = dossier.fingerprint
        dossier.quality_binding = expected
        return dossier

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def read(self, source_loc):
        selected = [e for e in self.elements if e['source_loc'] == source_loc]
        if not selected:
            raise KeyError(source_loc)
        return '\n'.join('\n'.join(self.lines[e['start_line']-1:e['end_line']]) for e in selected)

    def range(self, start_line, end_line):
        if (type(start_line) is not int or type(end_line) is not int
                or not 1 <= start_line <= end_line <= len(self.lines)):
            raise ValueError('invalid line range')
        return '\n'.join(self.lines[start_line-1:end_line])

    def sam_file_get(self, uri):
        asset = next((a for a in self.document['assets'] if a['uri'] == uri), None)
        if asset is None:
            raise PermissionError('object outside task evidence')
        path = Path(asset['local_path']).resolve()
        if not path.is_relative_to(self.objects_root / self.file_hash):
            raise PermissionError('object outside task evidence')
        data = path.read_bytes()
        if sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('object hash mismatch')
        return {'uri': uri, 'bytes': data, 'sha256': asset['sha256'],
                'original_uri': asset['original_uri']}
