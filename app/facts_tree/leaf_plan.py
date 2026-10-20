"""Opt-in file-driven orchestration. Only the employee selects leaves."""
from copy import deepcopy
import json

from ..document_read.dossier import Dossier
from ..employees.worker import EmployeeRunner
from ..memory.company_facts.l2_consolidation import ConsolidationService
from .leaf_build import LeafBuildService
from .leaf_contract import parse_employee_leaf
from .leaf_store import LeafPublishService, LeafRejected
from .quote_sources import DossierQuoteResolver
from .rollup import rollup_trunk, rollup_tree
from .template_packs import load_pack
from .tree_meta import require_enabled


class LeafPlanningService:
    def __init__(self, db_path, memory, *, worker=None, documents):
        # Construction must be inert, including when the feature is disabled.
        self.db_path, self.memory = db_path, memory
        self.worker = worker
        self.documents = deepcopy(dict(documents))
        self.store = None
        self.failed_leaves = []

    def _fragments(self, store, company_id):
        fragments = []
        for sha, document in self.documents.items():
            # Reuse the company/source guard; never discover additional files.
            _, names = store._sources(company_id, [sha])
            dossier = Dossier(document, file_hash=sha, objects_root='.')
            for loc in dict.fromkeys(e['source_loc'] for e in dossier.elements):
                fragments.append(dict(file_hash=dossier.file_hash, original_name=next(iter(names)),
                                      source_loc=loc, md=dossier.read(loc)))
        return fragments

    def _declared_sources(self, company_id):
        declared = []
        for sha in self.documents:
            _, names = self.store._sources(company_id, [sha])
            declared.append(dict(file_hash=sha, original_name=next(iter(names))))
        return declared

    def _prepare(self, company_id):
        require_enabled()
        if self.store is not None:
            if self.store.company_id != company_id:
                raise ValueError('company differs from injected host scope')
            return
        self.resolver = DossierQuoteResolver(self.documents)
        self.store = LeafPublishService(self.db_path, self.memory, quote_resolver=self.resolver,
                                        company_id=company_id)
        try:
            binding = self.store.binding.get(company_id)
            if binding is None:
                raise LeafRejected('company has no template binding')
            self.pack = load_pack(binding['template_pack'])
            if self.pack.version != binding['pack_version']:
                raise LeafRejected('bound pack version unavailable')
            self.leaves = {leaf.code: leaf for leaf in self.pack.leaves}
            self.fragments = self._fragments(self.store, company_id)
            self.builder = LeafBuildService(self.store, quote_resolver=self.resolver)
            if self.worker is None:
                self.worker = EmployeeRunner()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    def _run(self, company_id, request):
        return self.worker.run(skill='fact-leaf', text=json.dumps(request, ensure_ascii=False),
                               company_id=company_id)

    def plan(self, company_id, job_id=None):
        self._prepare(company_id)
        planned = []
        diag = dict(rejected_plans=[], unresolved=[], conflicts=[])
        directories = {trunk['code']: trunk['directory'] for trunk in self.pack.trunks}
        candidates = self._run(company_id, dict(action='plan', company_id=company_id,
            documents=self.fragments,
            template=[dict(trunk_code=leaf.trunk, trunk_directory=directories[leaf.trunk],
                           leaf_code=leaf.code, title=leaf.title, shape=leaf.shape)
                      for leaf in self.pack.leaves]))
        if not candidates:
            diag['unresolved'].append({'action': 'plan', 'reason': 'employee returned no plan'})
            return planned, diag
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get('leaves'), list):
                diag['rejected_plans'].append({'plan': candidate, 'reason': 'invalid plan shape'})
                continue
            conflicts = candidate.get('conflicts', [])
            if isinstance(conflicts, list):
                diag['conflicts'].extend(deepcopy(conflicts))
            else:
                diag['rejected_plans'].append({'plan': conflicts, 'reason': 'invalid conflicts shape'})
            for item in candidate['leaves']:
                code = item.get('leaf_code') if isinstance(item, dict) else None
                trunk = item.get('trunk') if isinstance(item, dict) else None
                leaf = self.leaves.get(code) if isinstance(code, str) else None
                if leaf is None or leaf.trunk != trunk or code.split('-')[1] != trunk:
                    diag['rejected_plans'].append({'plan': item, 'reason': 'invalid leaf/trunk'})
                elif code not in planned:
                    planned.append(code)
        if not planned:
            diag['unresolved'].append({'action': 'plan', 'reason': 'no valid leaves'})
        return planned, diag

    def build_one(self, company_id, leaf_code, job_id=None):
        self._prepare(company_id)
        try:
            leaf, _ = self.store._leaf(company_id, leaf_code)
            facts = ConsolidationService(self.db_path).list_verified_facts(company_id=company_id)
            inputs = [dict(fact_id=f['id'], entity=f['entity'], attribute=f['attribute'],
                           period=f['period'], value=f['value'], unit=f['unit'],
                           source=dict(file_hash=f['source_file'], source_page=f['source_page'],
                                       source_span=f['source_span']))
                      for f in facts if f['module_key'] == leaf.trunk]
            # Transport/budget exceptions are not business rejections.
            candidates = self._run(company_id, dict(action='leaf', company_id=company_id,
                target=dict(trunk=leaf.trunk, leaf_code=leaf_code, title=leaf.title, shape=leaf.shape),
                fragments=self.fragments, facts=inputs, declared_sources=self._declared_sources(company_id)))
            if len(candidates) != 1:
                raise LeafRejected('employee must return one leaf payload')
            payload = parse_employee_leaf(candidates[0])
            if payload['leaf_code'] != leaf_code:
                raise LeafRejected('employee leaf target mismatch')
            if payload.get('empty') is True:
                return dict(leaf_code=leaf_code, empty=True, note=payload['note'])
            if not set(payload['fact_ids']) <= {f['fact_id'] for f in inputs}:
                raise LeafRejected('fact reference outside supplied trunk facts')
            if not set(payload['source_files']) <= self.documents.keys():
                raise LeafRejected('source outside task evidence')
            built = self.builder.build_leaf(company_id=company_id, leaf_code=leaf_code,
                                           employee_payload=payload, job_id=job_id)
            return dict(leaf_code=leaf_code, **built)
        except LeafRejected as exc:
            self.failed_leaves.append(dict(leaf_code=leaf_code, reason=str(exc)))
            return None

    def finalize(self, company_id, affected_trunks):
        self._prepare(company_id)
        result = dict(rolled_up_trunks=[], tree_rolled=False)
        try:
            for trunk in dict.fromkeys(affected_trunks):
                rollup_trunk(leaf_store=self.store, company_id=company_id, trunk=trunk)
                result['rolled_up_trunks'].append(trunk)
            if result['rolled_up_trunks']:
                rollup_tree(leaf_store=self.store, company_id=company_id)
                result['tree_rolled'] = True
        except Exception as exc:
            result['rollup_error'] = str(exc)
            exc.planning_result = result
            raise
        return result

    def plan_and_build(self, *, company_id, job_id=None):
        require_enabled()
        result = dict(planned=[], built=[], noop=[], rejected_plans=[], failed_leaves=[],
                      rolled_up_trunks=[], tree_rolled=False, unresolved=[], conflicts=[])
        self.failed_leaves = []
        try:
            result['planned'], diag = self.plan(company_id, job_id)
            result.update(diag)
            affected = []
            for code in result['planned']:
                entry = self.build_one(company_id, code, job_id)
                if entry is None:
                    result['failed_leaves'].append(self.failed_leaves[-1])
                    continue
                if entry.get('empty') is True:
                    result.setdefault('empty', []).append(entry)
                    continue
                result['built'].append(entry)
                if not entry['changed']:
                    result['noop'].append(entry)
                trunk = self.leaves[code].trunk
                if trunk not in affected:
                    affected.append(trunk)
            try:
                result.update(self.finalize(company_id, affected))
            except Exception as exc:
                result.update(exc.planning_result)
                exc.planning_result = result
                raise
            return result
        finally:
            self.close()
