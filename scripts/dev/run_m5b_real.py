#!/usr/bin/env python3
"""One-click opt-in real-model acceptance; never invokes the live upload chain.

No business selectors or expected answers: print every model fact verbatim.
No OpenClaw gateway is needed for bounded direct-model requests. A bind:0
listener reserves an isolated gateway port for the entire run, so neither
this process nor its children can accidentally resolve the production port.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def print_json(label, value):
    print(label + ': ' + json.dumps(value, ensure_ascii=False), flush=True)


def baseline_module():
    spec = importlib.util.spec_from_file_location('m5b_baseline', ROOT/'scripts/db_baseline.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Import standalone read-only code; no application bootstrap/migration.
    return module


def configure(root, port):
    from tests.isolation_guard import data_keys
    for key in data_keys(os.environ):
        os.environ[key] = str(root/key.lower())
    paths = {'SAM_DATA_ROOT':root, 'SAM_MAIN_DB':root/'reader.db',
             'SAM_2A_ROOT':root/'evidence', 'SAM_OBJECTS_DIR':root/'objects',
             'SAM_OPENCLAW_STATE_DIR':root/'gateway',
             'SAM_OPENCLAW_CONFIG':root/'gateway/config.json'}
    for key,value in paths.items(): os.environ[key] = str(value)
    os.environ['SAM_OPENCLAW_GATEWAY_PORT'] = str(port)
    os.environ['OPENCLAW_GATEWAY_PORT'] = str(port)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['TMPDIR'] = str(root/'tmp')
    (root/'tmp').mkdir()
    tempfile.tempdir = str(root/'tmp')
    # Torch/HF caches never write the repository's pinned read-only model cache.
    for key in ('XDG_CACHE_HOME','HF_HOME','TORCH_HOME'):
        os.environ[key] = str(root/'cache'/key.lower())
    os.environ.setdefault('SAM_EMPLOYEE_MAX_OUTPUT_TOKENS','8192')
    os.environ.setdefault('SAM_LEADER_MODEL','qwen3.8-flash')
    os.environ['SAM_MODEL_TRACE_ROOT'] = str(root/'model-trace')
    key_env = os.environ.get('SAM_MODEL_API_KEY_ENV','SAM_LEADER_MODEL_API_KEY')
    if not os.environ.get(key_env):
        legacy = os.environ.get('SAM_GUIDE_MODEL_API_KEY')
        secret = Path.home()/'.bl_tokenplan_key'
        if legacy: os.environ[key_env] = legacy
        elif secret.is_file(): os.environ[key_env] = secret.read_text().strip()
    if not os.environ.get(key_env):
        raise RuntimeError(f'set {key_env} or provide ~/.bl_tokenplan_key')


def protect(root):
    from tests.isolation_guard import install, self_check
    self_check(os.environ, root)
    install([ROOT/'data', Path('/tmp/sam-m5')], root)
    # Native children also see a read-only host. Only this new root is writable.
    original = subprocess.Popen
    class Child(original):
        def __init__(self, args, *positional, **kwargs):
            if kwargs.get('shell') or isinstance(args,(str,bytes)):
                raise PermissionError('real runner only accepts explicit subprocess argv')
            super().__init__(['/usr/bin/bwrap','--die-with-parent','--ro-bind','/','/',
                '--bind',str(root),str(root),'--proc','/proc','--dev','/dev','--', *args],
                *positional, **kwargs)
    subprocess.Popen = Child


def run(args, root):
    from app.db.migrate import main as migrate
    from app.db.database import connect
    from app.runtime_config import RuntimeConfig
    from app.document_quality.sandbox import QualitySandbox
    from app.document_read.dossier import Dossier
    from app.document_read.quality import verify_dossier
    from app.document_read.service import DirectReadService
    from app.employees.model_tool import EmployeeModelTool
    from app.memory.provider import LocalVectorMemoryProvider
    from app.memory.company_facts.paths import brief_md_uri, guide_md_uri

    db = root/'reader.db'
    migrate(['--db', str(db)])
    config = replace(RuntimeConfig.from_env(), main_db=db, objects_dir=root/'objects')
    source = args.source.resolve()
    digest = sha256(source.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location('m5b_converter', ROOT/'skills/document-ingest/scripts/docx_fidelity.py')
    converter = importlib.util.module_from_spec(spec);spec.loader.exec_module(converter)
    document = converter.project_docx(source, digest, root/'objects')
    (root/'document.md').write_text(document['markdown'])
    for key in ('document_map','assets','self_check'):
        (root/(key+'.json')).write_text(json.dumps(document[key],ensure_ascii=False,indent=2))
    dossier = Dossier(document,file_hash=digest,objects_root=root/'objects')
    quality = verify_dossier(dossier, source=source,
        sandbox=QualitySandbox(ROOT/'.sam-isolated/venv',temporary_root=root/'quality'),
        company_id='acc-m5b-real',task_id='m5b-real')
    print_json('独立断网质检',quality)
    # Reads all real objects via the task-scoped retrieval channel, no silent omission.
    objects=[{'uri':a['uri'],'bytes':len(dossier.sam_file_get(a['uri'])['bytes'])} for a in document['assets']]
    print_json('对象原位链接与取回',objects)
    now = datetime.now(timezone.utc).isoformat()
    job_id,company = 'm5b-real','acc-m5b-real'
    with connect(db) as conn:
        conn.execute('INSERT INTO source_files(file_hash,original_name,size_bytes,storage_path,uploaded_at) VALUES(?,?,?,?,?)',
                     (digest,source.name,source.stat().st_size,str(source),now))
        conn.execute('INSERT INTO parse_jobs(job_id,company_id,file_hash,original_name,file_format,harness_format,size_bytes,created_at,updated_at,quality_status,quality_script_calls) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (job_id,company,digest,source.name,'docx','docx',source.stat().st_size,now,now,'pass',1))
    model = EmployeeModelTool(config)
    if args.inject_bad:
        delegate = model
        class InjectBad:
            injected = False
            def execute(self, **kwargs):
                raw = delegate.execute(**kwargs)
                result = json.loads(raw)
                if json.loads(kwargs['text'])['action']=='extract' and not self.injected:
                    # Structural malformed record, not a synthetic business assertion.
                    result['facts'].append({'source_loc':None})
                    self.injected = True
                return result
        model = InjectBad()
    memory = LocalVectorMemoryProvider(db,company_id=company)
    try:
        reader = DirectReadService(db,config=config,dossier=dossier,memory=memory,model=model)
        limits = {'classify':args.classify_calls,'attribute':args.attribute_calls,'extract':args.fact_calls}
        if args.budget_demo:
            partial = reader.run(company_id=company,job_id=job_id,limits={**limits,'extract':1})
            with connect(db) as conn:
                saved = [dict(r) for r in conn.execute('SELECT * FROM facts WHERE company_id=?',(company,))]
            print_json('低预算断点',{'status':partial['status'],'verified_facts_retained':len(saved),
                       'calls':partial['calls'],'resume_job':job_id})
            if partial['status']!='partial' or not saved:
                print_json('低预算演示未通过（不伪称保留成功）',partial.get('stage_failure') or partial['status'])
                return 2
        state = reader.run(company_id=company,job_id=job_id,limits=limits)
        with connect(db) as conn:
            facts=[dict(r) for r in conn.execute('SELECT * FROM facts WHERE company_id=? ORDER BY id',(company,))]
            todos=[dict(r) for r in conn.execute('SELECT * FROM todos WHERE company_id=? ORDER BY id',(company,))]
            job=dict(conn.execute('SELECT * FROM parse_jobs WHERE job_id=?',(job_id,)).fetchone())
        # Print ALL facts: no backend finance keyword/attribute mapping or answer oracle.
        printable = [{**fact, **json.loads(fact['source_span'])} for fact in facts]
        print_json('全部事实（含金额、占比与年份，供饭团对答案）',printable)
        print_json('保留疑点与未解决条目',todos)
        print_json('三阶段真实模型往返（事实阶段含简报/导览）',state['calls'])
        def optional_read(uri):
            try: return memory.read(uri)
            except FileNotFoundError: return ''
        brief=optional_read(brief_md_uri(company));guide=optional_read(guide_md_uri(company))
        print_json('终态',{'job':job['status'],'l2_committed':job['l2_committed'],
                          'brief_generated':bool(brief),'guide_generated':bool(guide),
                          'malformed_records':len(state['unresolved']),
                          'model':os.environ.get(config.model_name_env,config.model_default)})
        (root/'result.json').write_text(json.dumps({'state':state,'job':job,'facts':facts,'todos':todos},ensure_ascii=False,indent=2))
        if job['status'] == 'partial':
            print_json('可恢复阶段失败（保留成果，未验收成功）',state.get('stage_failure') or job['message'])
            return 2
        if args.inject_bad and not state['unresolved']:
            raise RuntimeError('malformed-record demonstration was not exercised')
        if job['status']!='done' or not job['l2_committed'] or not brief or not guide:
            raise RuntimeError('acceptance chain is incomplete; inspect retained artifacts')
        return 0
    finally:
        memory.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path('/tmp/sam-m5/material/fmt_C_info.docx'))
    parser.add_argument('--budget-demo',action='store_true')
    parser.add_argument('--inject-bad',action='store_true')
    parser.add_argument('--classify-calls',type=int,default=2)
    parser.add_argument('--attribute-calls',type=int,default=2)
    parser.add_argument('--fact-calls',type=int,default=16)
    args=parser.parse_args(argv)
    # Root is always fresh, never caller-controlled or reused; retain for blackbox review.
    baseline = baseline_module()
    protected = [ROOT/'data/app.db', Path('/tmp/sam-m5/main.db'), args.source.resolve()]
    before = [baseline.snapshot(path) for path in protected]
    root=Path(tempfile.mkdtemp(prefix='sam-m5b-real-'))
    (root/'before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2))
    print_json('隔离产物目录（保留，不删原件）',str(root))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen(1)
        port=listener.getsockname()[1]
        if port==18790: raise RuntimeError('refusing production gateway port')
        configure(root,port)
        print_json('隔离网关端口',{'port':port,'mode':'reserved; direct model; no gateway process started'})
        protect(root)
        try:
            status = run(args,root)
        finally:
            after = [baseline.snapshot(path) for path in protected]
            (root/'after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2))
            print_json('真库与原件前后完全相同',before==after)
            if before != after:
                raise RuntimeError('protected database/original baseline changed')
    return status


if __name__=='__main__':
    raise SystemExit(main())
