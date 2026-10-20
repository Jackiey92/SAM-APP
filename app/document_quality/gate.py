"""Document processor dependency gate: host guards, employee decisions.

No rule identifies a company, year, number or fact. Python actions are granted
only here to file_processor, never through global OpenClaw codeMode.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from .sandbox import QualitySandbox, SandboxFailure, conclusion
from ..employees.budget import AttributionBudget, CURRENT_PARSE_BUDGET
from ..employees.model_tool import EmployeeModelTool
from ..employees.worker import EmployeeRunner
from ..runtime_config import RuntimeConfig
from ..storage.mapping_store import render_mapping


class QualityNotPassed(RuntimeError):
    error_kind = 'document_quality'

    def __init__(self, result):
        self.result = result
        super().__init__('文档转换质量尚未通过，已保留原件及带坐标问题清单。')


class DocumentQualityGate:
    def __init__(self, *, config=None, sandbox=None, employee=None, max_model_calls=None,
                 timeout=240, max_reworks=None):
        self.config = config or RuntimeConfig.from_env()
        self.sandbox = sandbox or QualitySandbox(self.config.project_root / '.sam-isolated/venv')
        self.employee = employee
        self.max_model_calls = int(max_model_calls if max_model_calls is not None else os.environ.get('SAM_QC_MAX_MODEL_CALLS', '8'))
        self.timeout = timeout
        self.max_reworks = int(max_reworks if max_reworks is not None else os.environ.get('SAM_QC_MAX_REWORKS', '1'))
        if self.max_reworks < 0 or self.max_reworks > 3:
            raise ValueError('quality rework limit must be between zero and three')

    def inspect(self, *, company_id, task_id, source, manifest, snapshots=None, maps_path=None,
                reparse=None, persist=None, cancel=None):
        source = Path(source)
        digest = manifest['file_hash']
        import hashlib
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError('original hash differs from document quality task')
        original_loc = f'sam://original/{digest}'
        budget = AttributionBudget(max_calls=self.max_model_calls, timeout=self.timeout)
        stats = {'phase': 'quality', 'model_calls': 0, 'script_calls': 0, 'reworks': 0}
        history, images, previous = [], [], []
        # Do not charge quality to the downstream aggregate budget. This gate
        # has its own attempts/time count, including invalid model responses.
        token = CURRENT_PARSE_BUDGET.set(None)
        try:
            for round_index in range(self.max_reworks + 1):
                if cancel:
                    cancel()
                document = manifest.get('document') or {}
                evidence = {'original.' + manifest['format']: source.read_bytes(),
                            'document.md': render_mapping(manifest).encode(),
                            'engine.json': json.dumps(manifest.get('engine_document') or {}, ensure_ascii=False).encode()}
                for name in ('document_map', 'assets', 'self_check', 'hyperlinks'):
                    evidence[name + '.json'] = json.dumps(document.get(name, {}), ensure_ascii=False).encode()
                object_files = {}
                # Artifact locations are checked against the host-owned object
                # store; employee output never gets to select arbitrary paths.
                for index, asset in enumerate(document.get('assets', [])):
                    path = Path(asset['local_path']).resolve()
                    if not path.is_relative_to((Path(self.config.objects_dir) / digest).resolve()) or not path.is_file():
                        raise SandboxFailure('document object outside injected object store')
                    name = f'objects/{index + 1}{path.suffix}'
                    evidence[name] = path.read_bytes()
                    if hashlib.sha256(evidence[name]).hexdigest() != asset['sha256']:
                        raise SandboxFailure('document object hash differs from asset manifest')
                    object_files[asset['uri']] = '/evidence/' + name
                page_files = {}
                if snapshots and maps_path:
                    page_dir = Path(maps_path) / digest[:2] / (digest + '.pages')
                    for page in snapshots.get('pages', []):
                        path = (page_dir / page['filename']).resolve()
                        if not path.is_relative_to(page_dir.resolve()) or not path.is_file():
                            raise SandboxFailure('invalid original snapshot path')
                        name = f"pages/{page['page']}.png"
                        evidence[name] = path.read_bytes()
                        page_files[page['page']] = name
                task = {'role': '文档处理员', 'objective': '核验原件到 Markdown 的转换保真，禁止业务事实认定',
                        'document': evidence['document.md'].decode(),
                        'document_map': document.get('document_map', {}),
                        'self_check': document.get('self_check', {}),
                        'snapshot_warnings': (snapshots or {}).get('warnings', []),
                        'evidence_files': ['/evidence/' + name for name in evidence],
                        'object_uri_files': object_files, 'pages': list(page_files),
                        'previous_issues': previous, 'verification_history': history}
                locs = [e['source_loc'] for e in document.get('document_map', {}).get('elements', [])]
                if not locs:
                    for page in manifest.get('pages', []):
                        locs += [i.get('source_loc', {}).get('locator') for i in page.get('text_items', [])]
                        locs += [i.get('source_loc', {}).get('locator') for i in page.get('tables', [])]
                locs = [original_loc] + [loc for loc in locs if loc]
                script_calls_this_round = 0
                while True:
                    if cancel:
                        cancel()
                    budget.consume()
                    stats['model_calls'] = budget.model_calls
                    skill = (self.config.skill_root / 'document-ingest/QUALITY.md').read_text()
                    prompt = json.dumps(task, ensure_ascii=False)
                    if self.employee:
                        raw = self.employee(skill_text=skill, text=prompt, images=images,
                                            company_id=company_id, task_id=task_id, timeout=budget.remaining())
                    else:
                        raw = EmployeeModelTool(self.config).execute(skill_text=skill, text=prompt,
                            images=images, model=(os.environ.get('SAM_QC_VISION_MODEL', 'qwen3.8-flash')
                                                 if images else os.environ.get('SAM_QC_MODEL')),
                            timeout=max(1, int(budget.remaining())))
                    if cancel:
                        cancel()
                    items = EmployeeRunner._payload(raw)
                    if len(items) != 1:
                        raise ValueError('quality requires exactly one action')
                    action = items[0]
                    if action.get('action') == 'verify' and set(action) == {'action', 'script'}:
                        stats['script_calls'] += 1
                        script_calls_this_round += 1
                        remaining = budget.remaining()
                        from dataclasses import replace
                        sandbox = QualitySandbox(self.sandbox.venv, temporary_root=self.sandbox.temporary_root,
                            limits=replace(self.sandbox.limits, wall_seconds=min(self.sandbox.limits.wall_seconds, remaining)))
                        result = sandbox.run(role='file_processor', company_id=company_id, task_id=task_id,
                                             script=action['script'], evidence=evidence, cancel=cancel)
                        history.append(result)
                        task['verification_history'] = history
                        task['last_script_result'] = result
                    elif action.get('action') == 'view_snapshot' and set(action) == {'action', 'page'}:
                        if type(action['page']) is not int or action['page'] not in page_files:
                            raise ValueError('snapshot is outside task page list')
                        images = ['data:image/png;base64,' + base64.b64encode(evidence[page_files[action['page']]]).decode()]
                        task['viewed_snapshot_page'] = action['page']
                    elif action.get('action') == 'conclude':
                        if script_calls_this_round < 1:
                            raise ValueError('quality conclusion requires direct-original verification')
                        result = conclusion({k: v for k, v in action.items() if k != 'action'})
                        for item in result['issues'] + result['defects']:
                            loc = item['source_loc']
                            if not any(loc == base or loc.startswith(base + '/') for base in locs):
                                raise ValueError('quality issue coordinate not present in this original')
                        break
                    else:
                        raise ValueError('invalid document quality action')
                stats['reworks'] = round_index
                result['metrics'] = dict(stats)
                result['history'] = list(history)
                if result['status'] == 'rework' and round_index < self.max_reworks and reparse:
                    if persist:
                        persist(manifest, result)
                    previous = result['issues']
                    manifest = reparse(previous)
                    if manifest.get('file_hash') != digest or manifest.get('parse_summary', {}).get('status') != 'parsed':
                        raise ValueError('reconversion failed or original changed')
                    continue
                if result['status'] == 'rework':
                    result['status'] = 'needs_human'
                    result['retry_exhausted'] = True
                from .. import settings
                if settings.SAM_FACTS_TREE and result['status']=='pass':
                    from ..document_read.dossier import Dossier
                    dossier = Dossier(manifest['document'], file_hash=digest, objects_root=self.config.objects_dir)
                    result['evidence_binding'] = dict(company_id=company_id, job_id=task_id,
                        file_hash=digest, document_fingerprint=dossier.fingerprint)
                return self._save(manifest, result, persist)
            raise AssertionError('unreachable quality round')
        except Exception as exc:
            # Cancellation is not an employee conclusion.
            from ..parse_jobs import ParseJobCanceled
            if isinstance(exc, ParseJobCanceled):
                raise
            result = {'status': 'needs_human', 'issues': [{'source_loc': original_loc,
                      'problem': str(exc)[:2000], 'expected': '人工核查转换与核验环境后再处理'}],
                      'defects': [], 'history': list(history), 'metrics': dict(stats)}
            return self._save(manifest, result, persist)
        finally:
            CURRENT_PARSE_BUDGET.reset(token)

    @staticmethod
    def _save(manifest, result, persist):
        manifest['document_quality'] = result
        if persist:
            persist(manifest, result)
        return result
