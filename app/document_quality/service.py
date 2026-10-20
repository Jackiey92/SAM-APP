"""Ingestion wiring: quality is a dependency, never a fact-writing step."""
from __future__ import annotations
from dataclasses import replace
import logging
from pathlib import Path
import time
import json
from ..db.database import connect
from ..storage.mapping_store import write_mapping
from ..storage.archive_paths import hash_relpath

from .gate import DocumentQualityGate, QualityNotPassed
from .snapshots import render_original_pages
from ..storage.archive_store import ArchiveFileStore

LOGGER = logging.getLogger(__name__)


def review_parsed_document(*, job, adapter, staging, staging_id, config, db_path,
                           objects_dir, timeout, cancel=None, gate=None, snapshot_renderer=None):
    config = replace(config, objects_dir=Path(objects_dir))
    gate = gate or DocumentQualityGate(config=config, timeout=timeout)
    deadline = time.monotonic() + timeout
    current = [staging_id]
    payload = staging.get(staging_id)['payload']
    source = ArchiveFileStore(objects_path=objects_dir, db_path=db_path).path_for(job['file_hash'])
    renderer = snapshot_renderer or render_original_pages
    snapshots = renderer(sandbox=gate.sandbox, source=source, file_hash=job['file_hash'],
                         company_id=job['company_id'], task_id=job['job_id'],
                         file_format=payload['format'], maps_path=staging.maps_path)

    def persist(manifest, result):
        staging.update_payload(current[0], {'document_quality': result, 'original_pages': snapshots})
        path = staging.maps_path / hash_relpath(job['file_hash'], mapping=True)
        write_mapping(path.with_suffix('.quality.json'), json.dumps(result, ensure_ascii=False, indent=2))
        metrics = result.get('metrics') or {}
        with connect(db_path) as conn:
            conn.execute("""UPDATE parse_jobs SET quality_status=?,quality_model_calls=?,quality_script_calls=?,
                         quality_reworks=?,quality_issues=? WHERE job_id=? AND company_id=? AND file_hash=?""",
                         (result['status'], metrics.get('model_calls', 0), metrics.get('script_calls', 0),
                          metrics.get('reworks', 0), json.dumps(result['issues'], ensure_ascii=False),
                          job['job_id'], job['company_id'], job['file_hash']))
        LOGGER.info('document quality job=%s company=%s status=%s metrics=%s',
                    job['job_id'], job['company_id'], result['status'], result.get('metrics'))

    def reparse(issues):
        if cancel:
            cancel()
        current[0] = adapter.run_parse(job['file_hash'], job['harness_format'],
                                     timeout=max(1, int(deadline - time.monotonic())), quality_issues=issues)
        return staging.get(current[0])['payload']

    gate.timeout = min(gate.timeout, max(1, deadline - time.monotonic()))
    result = gate.inspect(company_id=job['company_id'], task_id=job['job_id'], source=source,
                          manifest=payload, snapshots=snapshots, maps_path=staging.maps_path,
                          persist=persist, reparse=reparse, cancel=cancel)
    if result['status'] != 'pass':
        raise QualityNotPassed(result)
    return current[0]
