#!/usr/bin/env python3
"""Non-interactive REAL upload -> employee -> local memory -> LEADER acceptance.

Run with PATH="$PWD/.venv/bin:$PATH" SAM_MEMORY_ROOT_URI=<product-root>
python3 scripts/check_2b_e2e.py --report /tmp/sam-2b-e2e.json

No fake employee/provider fallback. A failed prerequisite/semantic assertion
is a failed run. Requires the six import business tables to be empty first;
never clears pre-existing records. Keeps resident memory, stops only process groups
started by this run, and removes only the unique acc-* acceptance scope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/company/谷斗科技-经营与财务摘要.xlsx"
BUSINESS_TABLES = ("source_files", "parse_jobs", "parse_staging", "file_classifications", "facts", "todos")
NAME = "谷斗科技（上海）有限公司"
ACCEPTANCE_PORTS = (18789, 18890)
STARTUP_TIMEOUT_SECONDS = 180


def counts(db):
    with sqlite3.connect(db) as conn:
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if table in tables else 0
                for table in BUSINESS_TABLES}


def numeric_literals(text):
    # Acceptance assertion only; never used for production classification,
    # filtering, attribution, aggregation, or numeric repair.
    return set(re.findall(r"(?<![\d.])\d+(?:\.\d+)?", text))


def assert_provenance(facts, brief, guide):
    by_id = {fact["id"]: fact for fact in facts}
    assert len(facts) >= 10, f"too few employee facts: {len(facts)}"
    assert all(fact["source_file"] and fact["source_page"] and fact["source_span"] for fact in facts)
    assert all(fact["entity"] == NAME for fact in facts), "employee changed host entity"
    required = {"1200", "180", "1500", "240", "星河制造有限公司", "青禾设备有限公司", "海川物流有限公司",
                "林舟", "周宁", "陈禾", "60", "25", "15", "上海市浦东新区", "TEST-GUDOU-ONLY"}
    stored = json.dumps(facts, ensure_ascii=False)
    assert all(value in stored for value in required), "L2 lost company information"
    entries = {item["entry_id"]: item for item in brief["items"]}
    assert entries and guide["items"]
    for item in entries.values():
        refs = item["fact_ids"]
        assert refs and all(ref in by_id for ref in refs)
        source_text = " ".join(str(by_id[ref].get(key) or "") for ref in refs
                               for key in ("entity", "attribute", "period", "value", "unit"))
        assert numeric_literals(item["conclusion"]) <= numeric_literals(source_text), item
        assert {source["fact_id"] for source in item["sources"]} == set(refs)
        for source in item["sources"]:
            fact = by_id[source["fact_id"]]
            assert (source["file_hash"], source["page"], source["span"]) == (
                fact["source_file"], fact["source_page"], fact["source_span"])
    l1_text = " ".join(item["conclusion"] for item in entries.values())
    assert "1500" in l1_text and "1200" in l1_text, "L1 lost cross-period finance"
    assert all(client in l1_text for client in ("星河制造有限公司", "青禾设备有限公司", "海川物流有限公司"))
    for item in guide["items"]:
        assert item["l1_entry_ids"] and all(ref in entries for ref in item["l1_entry_ids"])
        cited = " ".join(entries[ref]["conclusion"] for ref in item["l1_entry_ids"])
        assert numeric_literals(item["conclusion"]) <= numeric_literals(cited), item
        # Non-numeric semantic subset is inspected in the recorded real output,
        # never approximated with a backend keyword/alias mapping.


def listener_pids(port):
    inodes = set()
    for table in (Path('/proc/net/tcp'), Path('/proc/net/tcp6')):
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].split(':')[-1], 16) == port:
                inodes.add(fields[9])
    pids = set()
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if any(fd.readlink().as_posix() in {f'socket:[{inode}]' for inode in inodes}
                   for fd in (process / 'fd').iterdir()):
                pids.add(int(process.name))
        except (OSError, PermissionError):
            continue
    if inodes and not pids:
        raise RuntimeError(f"cannot verify listener on {port}; refusing to kill unknown process")
    return pids


def check_flask_port_available():
    if listener_pids(ACCEPTANCE_PORTS[0]):
        raise RuntimeError('port 18789 is already occupied; refusing to stop a process not started by this run')


def process_group_alive(pgid):
    """Exited leaders may leave live workers; zombies need no further signal."""
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                return True
        except FileNotFoundError:
            continue
    return False


def stop_process_groups(processes):
    """Signal only our Popen(start_new_session=True) groups, even after exit."""
    from app.runtime_config import PROCESS_STOP_GRACE_SECONDS, PROCESS_STOP_POLL_SECONDS
    errors = []
    for process in reversed(processes):
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + PROCESS_STOP_GRACE_SECONDS
                while process_group_alive(process.pid) and time.monotonic() < deadline:
                    process.poll()  # reap the leader; do not mistake it for the group
                    time.sleep(PROCESS_STOP_POLL_SECONDS)
                if not process_group_alive(process.pid):
                    break
            process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
            if process_group_alive(process.pid):
                raise RuntimeError(f'process group {process.pid} did not stop')
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError('; '.join(errors))


def wait_acceptance_ports_closed():
    from app.runtime_config import PROCESS_STOP_GRACE_SECONDS, PROCESS_STOP_POLL_SECONDS
    deadline = time.monotonic() + PROCESS_STOP_GRACE_SECONDS
    while True:
        occupied = {}
        for port in ACCEPTANCE_PORTS:
            pids = listener_pids(port)
            if pids:
                occupied[port] = sorted(pids)
        if not occupied:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'acceptance ports still occupied: {occupied}; private state retained')
        time.sleep(PROCESS_STOP_POLL_SECONDS)


def terminate_acceptance(signum, frame):
    # An external TERM/INT must unwind finally rather than orphan the session.
    raise SystemExit(128 + signum)


def http_json(base, path, payload=None, *, timeout=15, multipart=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {} if payload is None else {'Content-Type': 'application/json'}
    if multipart is not None:
        data, headers['Content-Type'] = multipart
    req = urllib.request.Request(base + path, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def upload_body(company):
    boundary = 'sam-' + uuid.uuid4().hex
    data = (f'--{boundary}\r\nContent-Disposition: form-data; name="company_id"\r\n\r\n{company}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{FIXTURE.name}"\r\n'
            'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n').encode()
    data += FIXTURE.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    return data, f'multipart/form-data; boundary={boundary}'


def cleanup_db(db, company, digest):
    # Only after every writer owned by this acceptance process is stopped.
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        for table in ('todos', 'facts', 'fact_runs', 'entity_bridge_blocks', 'entity_bridge_artifacts',
                      'entity_bridge_runs', 'file_classifications', 'source_edits', 'parse_jobs', 'entity_roster'):
            conn.execute(f'DELETE FROM {table} WHERE company_id=?', (company,))
        conn.execute('DELETE FROM parse_staging WHERE file_hash=?', (digest,))
        conn.execute('DELETE FROM file_tags WHERE file_hash=?', (digest,))
        conn.execute('DELETE FROM source_files WHERE file_hash=?', (digest,))


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=Path('/tmp/sam-2b-e2e.json'))
    parser.add_argument('--timeout', type=int, default=3600,
                        help='hard acceptance budget, including bounded job retries')
    parser.add_argument('--job-retries', type=int, choices=(0, 1, 2), default=1,
                        help='retry a terminal employee_transport failure, never a semantic assertion')
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    report = {'mode': 'real-local-memory-and-model', 'status': 'failed', 'fixture': str(FIXTURE)}
    server = memory = config = None
    processes = []
    prepared = False
    acceptance_root = None
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    company = 'acc-gudou-' + uuid.uuid4().hex[:10]
    scratch = ROOT / 'data' / ('acc-runtime-' + company)
    report['company_id'] = company
    sys.path.insert(0, str(ROOT))
    previous_handlers = {sig: signal.signal(sig, terminate_acceptance) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        # Fail before writes if this execution sandbox forbids network sockets.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
            pass
        if os.environ.get('SAM_CLOUD_READONLY') == '1':
            raise RuntimeError('real acceptance cannot run in cloud-readonly mode')
        acceptance_root = 'sam://facts/' + company
        os.environ.update(SAM_COMPANY_ID=company,
                          SAM_MAIN_DB=str(ROOT / 'data/app.db'), SAM_DATA_ROOT=str(ROOT / 'data'),
                          SAM_2A_ROOT=str(ROOT / 'data/2a'),
                          SAM_OPENCLAW_STATE_DIR=str(scratch / 'state'),
                          SAM_OPENCLAW_CONFIG=str(scratch / 'state/openclaw.json'),
                          SAM_OPENCLAW_GATEWAY_PORT='18890', OPENCLAW_GATEWAY_PORT='18890',
                          PORT='18789', HOST='127.0.0.1')
        os.environ['PATH'] = str(ROOT / '.venv/bin') + ':' + os.environ.get('PATH', '')
        if not os.environ.get('SAM_LEADER_MODEL_API_KEY'):
            key_file = Path.home() / '.bl_tokenplan_key'
            if key_file.exists():
                os.environ['SAM_LEADER_MODEL_API_KEY'] = key_file.read_text().strip()
        if not os.environ.get('SAM_LEADER_MODEL_API_KEY'):
            raise RuntimeError('missing Token Plan key; no mock fallback')
        os.environ.setdefault('SAM_LEADER_MODEL_BASE_URL', 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1')
        os.environ.setdefault('SAM_LEADER_MODEL', 'qwen3.8-max')
        from app.runtime_config import RuntimeConfig
        from app.db.migrate import main as migrate
        from app.providers import memory_provider
        from app.memory.company_facts.paths import brief_json_uri, brief_md_uri, guide_json_uri, guide_md_uri, fact_uri
        from app.context_assembler import ContextAssembler
        from app.memory_map import source_catalog_from_db, MemoryMapTools
        from app.storage import ArchiveFileStore
        from app.storage.mapping_store import SourceMapService
        config = RuntimeConfig.from_env(project_root=ROOT)
        from app.employee_policy import call_timeout, employee_total_timeout, parse_total_timeout
        report['budgets'] = {'calls': {skill: call_timeout(skill) for skill in (
            'document-classifier', 'finance-fact-extraction', 'fact-brief', 'fact-guide')},
            'employee_total': employee_total_timeout(), 'parse_total': parse_total_timeout(),
            'acceptance_total': args.timeout}
        report['before'] = counts(config.main_db)
        assert not any(report['before'].values()), 'business data is not empty; refusing destructive reset'
        migrate(["--db", str(config.main_db)])
        check_flask_port_available()
        scratch.mkdir(parents=True, exist_ok=True)
        # Same full startup steps as run.sh, with a private Gateway state/port
        # so resident employee sessions/configs cannot contaminate acceptance.
        with (scratch / 'startup.log').open('w') as output:
            for script in ('scripts/prepare-openclaw-runtime.py',):
                process = subprocess.Popen([sys.executable, script], cwd=ROOT, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                processes.append(process)
                code = process.wait(timeout=STARTUP_TIMEOUT_SECONDS)
                if code:
                    raise subprocess.CalledProcessError(code, script)
                if not process_group_alive(process.pid):
                    # Do not retain dead startup groups for a 15-minute run:
                    # that numeric PGID could eventually be reused elsewhere.
                    processes.remove(process)
        memory = memory_provider(config)
        report['memory_backend'] = config.memory_provider
        log = (scratch / 'flask.log').open('w')
        server = subprocess.Popen([sys.executable, 'webapp/app.py'], cwd=ROOT, stdout=log,
                                  stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(server)
        log.close()
        deadline = time.monotonic() + args.timeout
        base = 'http://127.0.0.1:18789'
        while not listener_pids(18789):
            assert server.poll() is None, 'Flask startup failed; inspect flask.log'
            assert time.monotonic() < deadline, 'Flask startup timeout'
            time.sleep(.25)
        prepared = True  # only this unique company/hash can now be written
        http_json(base, '/api/entity-roster', {'entity_name': NAME, 'aliases': ['谷斗科技']})
        job = http_json(base, '/api/upload', multipart=upload_body(company))
        assert job['file_hash'] == digest
        report['upload'] = job
        report['job_attempts'] = []
        while True:
            job = http_json(base, f"/api/parse-jobs/{job['job_id']}?company_id={company}")['job']
            if job['status'] in {'done', 'failed', 'canceled'}:
                report['job_attempts'].append(job)
                if (job['status'] == 'failed' and job.get('error_kind') == 'employee_transport'
                        and len(report['job_attempts']) <= args.job_retries
                        and time.monotonic() + 5 < deadline):
                    time.sleep(2)
                    # The worker has reported terminal state; wait for its
                    # finally cleanup before creating a retry generation.
                    from urllib.error import HTTPError
                    for _ in range(2):
                        try:
                            job = http_json(base, f"/api/parse-jobs/{job['job_id']}/retry",
                                            {'company_id': company})['job']
                            break
                        except HTTPError as exc:
                            if exc.code != 409:
                                raise
                            time.sleep(1)
                    else:
                        raise RuntimeError('terminal worker cleanup did not finish before retry')
                    continue
                break
            assert time.monotonic() < deadline, f'parse timeout: {job}'
            time.sleep(1)
        report['parse_job'] = job
        assert job['status'] == 'done', job
        with sqlite3.connect(config.main_db) as conn:
            conn.row_factory = sqlite3.Row
            facts = [dict(row) for row in conn.execute('SELECT * FROM facts WHERE company_id=?', (company,))]
            todos = [dict(row) for row in conn.execute('SELECT * FROM todos WHERE company_id=?', (company,))]
        report.update(l2_count=len(facts), todo_count=len(todos), l2=facts, todos=todos)
        assert todos and all(row['reason'] and row['source_span'] and row['handoff_json'] for row in todos)
        assert any('987.65' in row['handoff_json'] or '432.10' in row['handoff_json'] for row in todos)
        maps = SourceMapService(config.main_db, config.objects_dir)
        assert maps.map_path(digest).is_file()
        stored = ArchiveFileStore(config.objects_dir, config.main_db).get(digest)
        assert (config.objects_dir / stored.storage_path).is_file()
        assert config.objects_dir.parent == ROOT / 'data/2a'
        objects = memory.query(prefix=acceptance_root)
        assert not any('2a_extraction' in str(row) or digest + '/L2/manifest.json' in str(row) for row in objects)
        report['source_storage'] = {'original': str(config.objects_dir / stored.storage_path),
                                    'mapping': str(maps.map_path(digest)), 'memory_has_2a_extraction': False}
        brief = json.loads(memory.read(brief_json_uri(company)))
        guide = json.loads(memory.read(guide_json_uri(company)))
        report.update(l1=brief, l0=guide, l1_md=memory.read(brief_md_uri(company)), l0_md=memory.read(guide_md_uri(company)))
        assert_provenance(facts, brief, guide)
        tools = MemoryMapTools(memory, ArchiveFileStore(config.objects_dir, config.main_db), company,
                               source_catalog=source_catalog_from_db(config.main_db))
        for fact in facts:
            assert memory.get_fact(company, str(fact['id'])) == fact
        for item in brief['items']:
            for source in item['sources']:
                assert json.loads(tools.memory_read(source['uri']))['id'] == source['fact_id']
        question = '谷斗去年（2025年度）营收多少、客户有哪些？只引用已验证 L1/L2 数字，不换算，不使用编号列表。'
        context = ContextAssembler(memory, company_id=company, thread_id='acceptance',
                                   source_catalog=source_catalog_from_db(config.main_db)).assemble(question)
        assert context['layers']['fact_guide'] == report['l0_md']
        report['leader_context_has_l0'] = True
        remaining = int(deadline - time.monotonic())
        assert remaining > 0, 'acceptance deadline exhausted before LEADER'
        report['leader_question'] = question
        answer = http_json(base, '/api/chat', {'company_id': company, 'thread_id': 'acceptance',
                                             'session_id': company, 'message': question}, timeout=min(650, remaining))
        report['leader'] = answer
        assert '1500' in answer['message']
        assert all(name in answer['message'] for name in ('星河制造', '青禾设备', '海川物流'))
        supported = numeric_literals(' '.join(item['conclusion'] for item in brief['items']))
        assert numeric_literals(answer['message']) <= supported, answer
        report['status'] = 'success'
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        # Ignore a second TERM/INT while bounded cleanup is already in flight.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            cleanup_errors = []
            runtime_stopped = False
            try:
                stop_process_groups(processes)
                wait_acceptance_ports_closed()
                runtime_stopped = True
            except Exception as exc:
                cleanup_errors.append(f'process cleanup: {exc}')
            if runtime_stopped and prepared and config is not None:
                try:
                    cleanup_db(config.main_db, company, digest)
                    from app.storage.archive_paths import hash_relpath
                    for path in (config.objects_dir / hash_relpath(digest),
                                 config.maps_dir / hash_relpath(digest, mapping=True),
                                 config.main_db.parent / f'runtime_outbox/unresolved/document_manager/{digest}.json',
                                 config.harness_root / 'inbox' / f'{digest}.json',
                                 config.harness_root / 'outbox' / f'{digest}.json'):
                        path.unlink(missing_ok=True)
                    report['after'] = counts(config.main_db)
                    assert not any(report['after'].values()), report['after']
                    report['external_2a_clean'] = not (config.objects_dir / hash_relpath(digest)).exists() and not (
                        config.maps_dir / hash_relpath(digest, mapping=True)).exists()
                    with sqlite3.connect(config.main_db) as conn:
                        report['scope_after'] = {table: conn.execute(
                            f'SELECT COUNT(*) FROM {table} WHERE company_id=?', (company,)).fetchone()[0]
                            for table in ('fact_runs', 'entity_bridge_blocks', 'entity_bridge_artifacts',
                                          'entity_bridge_runs', 'entity_roster')}
                    assert not any(report['scope_after'].values()), report['scope_after']
                except Exception as exc:
                    cleanup_errors.append(f'ledger/source cleanup: {exc}')
            if runtime_stopped and memory is not None and acceptance_root:
                try:
                    try:
                        memory.delete(acceptance_root, recursive=True)
                        scoped = memory.scoped(company) if hasattr(memory, 'scoped') else memory
                        for domain in ('cap_table', 'derivation', 'conversations'):
                            scoped.delete(f'sam://kv/co/{company}/{domain}', recursive=True)
                        memory.delete(f'sam://kv/rt/{company}/conversation', recursive=True)
                    except FileNotFoundError:
                        pass
                    try:
                        remaining = memory.query(prefix=acceptance_root)
                    except FileNotFoundError:
                        remaining = []
                    assert not remaining, remaining
                    report['memory_clean'] = True
                except Exception as exc:
                    cleanup_errors.append(f'Memory cleanup: {exc}')
            # Keep concise transport diagnostics even after scope cleanup; never
            # embed credentials, command prompts, or entire gateway transcripts.
            from app.runtime_errors import compact_error
            log_path = scratch / 'flask.log'
            try:
                if log_path.exists():
                    report['runtime_diagnostics'] = [compact_error(line) for line in log_path.read_text(
                        encoding='utf-8', errors='replace').splitlines()
                        if any(marker in line for marker in ('employee retry skill=', 'Memory write', 'timeoutPhase=', 'parse job future failed'))][-20:]
                    saved_log = args.report.with_suffix('.flask.log')
                    saved_log.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(log_path, saved_log)
                    report['runtime_log'] = str(saved_log)
            except OSError as exc:
                cleanup_errors.append(f'log diagnostics: {exc}')
            if config is not None and config.config_path.exists():
                try:
                    report['openclaw_timeout_seconds'] = json.loads(config.config_path.read_text())['agents']['defaults']['timeoutSeconds']
                except (OSError, ValueError, KeyError) as exc:
                    cleanup_errors.append(f'config diagnostics: {exc}')
            # Keep evidence outside data/, remove private acceptance sessions too.
            if scratch.exists() and not cleanup_errors:
                try:
                    # Recheck immediately before removing the process identity/state.
                    wait_acceptance_ports_closed()
                    shutil.rmtree(scratch)
                    report['private_runtime_clean'] = not scratch.exists()
                except (OSError, RuntimeError) as exc:
                    cleanup_errors.append(f'private runtime cleanup: {exc}')
            if cleanup_errors:
                report['cleanup_errors'] = cleanup_errors
                report['status'] = 'failed'
            report['elapsed_seconds'] = round(time.monotonic() - started, 2)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({key: report[key] for key in ('status', 'error', 'l2_count', 'todo_count', 'after', 'memory_clean', 'cleanup_errors')
                              if key in report}, ensure_ascii=False))
            print('report=' + str(args.report))
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    return 0 if report['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
