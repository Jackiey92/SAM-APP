"""Assembly uses synthetic 谷斗 facts; no network or real diligence corpus."""
from dataclasses import replace
import json
import subprocess

import pytest

from app.providers import memory_provider
from app.runtime_config import RuntimeConfig
from app.memory.provider import LocalVectorMemoryProvider
from app.db.database import init_db
from app.memory.company_facts.l2_consolidation import ConsolidationService
from app.memory.company_facts.l2_fact_extractor import ExtractedFact
from app.memory.company_facts.l2_projection import L2ProjectionService
from app.memory.company_facts.l1_brief import L1BriefService
from app.memory.company_facts.l0_guide import L0GuideService
from app.memory import paths
from app.memory_map import MapBuilder
from app.context_assembler import ContextAssembler
from app.thread_manager import ThreadManager
from app.null_memory import NullMemoryProvider
from app.storage import ArchiveFileStore


class SyntheticEmbedding:
    dim, embedder, model_version = 2, 'synthetic', 'v1'
    def embed_texts(self, texts): return [[1., 1.] for _ in texts]
    def embed_query(self, query): return [1., 1.]


def configuration(tmp_path):
    config = RuntimeConfig.from_env(env={'SAM_MEMORY_ROOT_URI': 'sam://sam-test/product-root',
                                        'SAM_MAIN_DB': str(tmp_path / 'ledger.db')})
    return config


def test_defaults_localvector_and_rejects_retired_backend(tmp_path, monkeypatch):
    monkeypatch.setattr('app.memory.provider.LocalBgeProvider', SyntheticEmbedding)
    config = configuration(tmp_path)
    init_db(config.main_db, embedding=SyntheticEmbedding())
    local = memory_provider(config, env={})
    assert isinstance(local, LocalVectorMemoryProvider)
    assert local.database == config.main_db
    source = ArchiveFileStore(tmp_path / 'objects', config.main_db).put_bytes(b'gudou', original_name='gudou.txt')
    result = ConsolidationService(config.main_db).consolidate_candidates([ExtractedFact(
        'acc-gudou', '员工指标', '2024', '3', 'number', '元', source.file_hash, 1,
        'row/1', 1., False, module_key='fin')])
    try:
        assert L2ProjectionService(config.main_db, local).publish(company_id='acc-gudou') == 1
        assert local.search('谷斗', prefix=paths.facts_uri('acc-gudou'))
        doc = paths.facts_uri('acc-gudou', None, 'guide.md')
        class Employee:
            def run(self, *, skill, text, **kwargs):
                if skill == 'fact-brief':
                    rows = json.loads(text)['facts']
                    return [{'topic': '员工主题', 'conclusion': '谷斗合成简报', 'fact_ids': [r['id'] for r in rows]}]
                return [{'conclusion': '谷斗合成导览', 'l1_entry_ids': ['brief-1']}]
        employee = Employee()
        assert L1BriefService(config.main_db, local, worker=employee).rebuild(company_id='acc-gudou')
        assert L0GuideService(local, worker=employee).rebuild(company_id='acc-gudou')
        local_guide = local.read(doc)
        assembled = ContextAssembler(local, company_id='acc-gudou', thread_id='reserved').assemble('谷斗')
        assert '谷斗合成导览' in assembled['prompt']
        assert assembled['layers']['conversation']['last'] == []
        assert ThreadManager(local).list('acc-gudou') == []
        assert MapBuilder(local).rebuild_map('acc-gudou').map['modules']
        assert ThreadManager(local).create('acc-gudou', 'reserved', title='synthetic')['thread_id'] == 'reserved'
    finally:
        local.close()
    with pytest.raises(ValueError, match="OV 已摘除"):
        memory_provider(config, env={'SAM_MEMORY_BACKEND': 'ov'})
    assert ConsolidationService(config.main_db).list_verified_facts(company_id='acc-gudou')[0]['id'] == result.verified_fact_ids[0]
    restored = memory_provider(config, env={})
    try: assert restored.read(doc) == local_guide
    finally: restored.close()


def test_cloud_bypasses_both_backends(tmp_path, monkeypatch):
    config = replace(configuration(tmp_path), cloud_readonly=True)
    monkeypatch.setattr('app.memory.provider.LocalBgeProvider', lambda: pytest.fail('cloud loaded model'))
    for backend in ('localvector', 'ov'):
        assert isinstance(memory_provider(config, env={'SAM_MEMORY_BACKEND': backend}), NullMemoryProvider)
    assert not config.main_db.exists()


def test_unknown_backend_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match='SAM_MEMORY_BACKEND'):
        memory_provider(configuration(tmp_path), env={'SAM_MEMORY_BACKEND': 'guess'})
