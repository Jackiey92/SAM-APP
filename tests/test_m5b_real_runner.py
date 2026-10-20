"""Offline contracts for the real runner. No sockets, subprocesses or models."""
from pathlib import Path


def test_skill_frontmatter_and_identity():
    from app.contracts.phase1 import build_skill_catalog
    catalog = build_skill_catalog(Path(__file__).resolve().parents[1]/'skills')
    skill = next(s for s in catalog['skills'] if s['name']=='file-processor')
    assert not catalog['rejected']
    assert skill['name']=='file-processor'


def load_script(name, relative):
    import importlib.util
    path=Path(__file__).resolve().parents[1]/relative
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_real_runner_help_never_allocates_port():
    import pytest
    runner=load_script('m5b_real','scripts/dev/run_m5b_real.py')
    with pytest.raises(SystemExit) as stopped:
        runner.main(['--help'])
    assert stopped.value.code==0


def test_independent_per_cell_check_rejects_swapped_columns(tmp_path):
    from docx import Document
    from hashlib import sha256
    import json
    converter=load_script('m5b_converter_test','skills/document-ingest/scripts/docx_fidelity.py')
    verifier=load_script('m5b_verifier_test','skills/document-ingest/scripts/verify_direct_docx.py')
    original=tmp_path/'original.docx'
    doc=Document();table=doc.add_table(rows=2,cols=3)
    for cell,value in zip(table.rows[0].cells,['项目','期间甲','期间乙']): cell.text=value
    for cell,value in zip(table.rows[1].cells,['测试量','12.00','15.00']): cell.text=value
    doc.save(original)
    document=converter.project_docx(original,sha256(original.read_bytes()).hexdigest(),tmp_path/'objects')
    evidence=tmp_path/'document.json'
    evidence.write_text(json.dumps(document))
    assert verifier.verify(tmp_path)['status']=='pass'
    document['markdown']=document['markdown'].replace('| 测试量 | 12.00 | 15.00 |','| 测试量 | 15.00 | 12.00 |')
    evidence.write_text(json.dumps(document))
    result=verifier.verify(tmp_path)
    assert result['status']=='rework' and result['issues'][0]['source_loc']


def test_object_retrieval_denies_external_path_and_changed_bytes(tmp_path):
    from hashlib import sha256
    import pytest
    from app.document_read.dossier import Dossier
    digest=sha256(b'original').hexdigest()
    allowed=tmp_path/'objects'/digest;allowed.mkdir(parents=True)
    asset=allowed/'asset.bin';asset.write_bytes(b'asset')
    record={'uri':'sam://object/test','local_path':str(asset),'sha256':sha256(b'asset').hexdigest(),'original_uri':'sam://original/test'}
    dossier=Dossier({'markdown':'asset','document_map':{'elements':[]},'assets':[record]},file_hash=digest,objects_root=tmp_path/'objects')
    assert dossier.sam_file_get(record['uri'])['bytes']==b'asset'
    with pytest.raises(PermissionError): dossier.sam_file_get('unknown')
    asset.write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash'): dossier.sam_file_get(record['uri'])
    record['local_path']=str(tmp_path/'other')
    with pytest.raises(PermissionError): dossier.sam_file_get(record['uri'])


def test_model_trace_preserves_raw_envelope_without_authorization(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from app.employees.model_tool import EmployeeModelTool
    import app.employees.model_tool as transport
    monkeypatch.setenv('SAM_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('SAM_MODEL_TRACE_ROOT',str(tmp_path/'trace'))
    monkeypatch.setenv('M5B_TEST_KEY','test-secret-not-for-trace')
    monkeypatch.setenv('M5B_TEST_URL','https://example.invalid/v1')
    envelope={'choices':[{'finish_reason':'stop','message':{'content':'{"facts":[]}'}}],
              'usage':{'prompt_tokens':5,'completion_tokens':3,'total_tokens':8}}
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps(envelope).encode()
    monkeypatch.setattr(transport.urllib.request,'urlopen',lambda *args,**kwargs:Response())
    config=SimpleNamespace(model_api_key_env='M5B_TEST_KEY',model_name_env='M5B_TEST_MODEL',
                           model_default='test-model',model_base_url_env='M5B_TEST_URL',model_base_url='')
    assert EmployeeModelTool(config)._request(skill_text='source-only',text='document',timeout=1)=='{"facts":[]}'
    files=list((tmp_path/'trace').iterdir())
    assert len(files)==3 and all('test-secret' not in p.read_text() for p in files)
    metrics=json.loads(next(p for p in files if p.name.endswith('.metrics.json')).read_text())
    assert metrics['usage']['total_tokens']==8 and metrics['finish_reason']=='stop' and not metrics['stream']
    response=next(p for p in files if p.name.endswith('.response.json'))
    assert json.loads(response.read_text())==envelope and response.stat().st_mode & 0o777 == 0o600


def test_model_trace_refuses_nonisolated_root(tmp_path, monkeypatch):
    import pytest
    from types import SimpleNamespace
    from app.employees.model_tool import EmployeeModelTool
    monkeypatch.setenv('SAM_DATA_ROOT',str(tmp_path/'isolated'))
    monkeypatch.setenv('SAM_MODEL_TRACE_ROOT',str(tmp_path/'outside'))
    monkeypatch.setenv('M5B_TEST_KEY','fake')
    config=SimpleNamespace(model_api_key_env='M5B_TEST_KEY',model_name_env='M5B_TEST_MODEL',
                           model_default='test',model_base_url_env='M5B_TEST_URL',model_base_url='https://example.invalid')
    with pytest.raises(ValueError,match='isolated'):
        EmployeeModelTool(config)._request(skill_text='s',text='t',timeout=1)
    assert not (tmp_path/'outside').exists()
