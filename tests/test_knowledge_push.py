"""Isolated push transport tests: no real cloud, database or credentials."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace
import urllib.error

import pytest

from scripts import sam_knowledge_push as push


@pytest.fixture
def push_env(monkeypatch):
    for name in ("SAM_COMPANY_ID", "SAM_KNOWLEDGE_CLOUD_URL", "SAM_KNOWLEDGE_INGEST_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAM_COMPANY_ID", "acc-push")
    monkeypatch.setenv("SAM_KNOWLEDGE_INGEST_KEY", "acc-test-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    payload = {
        "company_id": "acc-push",
        "evidence": {"files": [{"l0": {"content": "中文原文"}}, {"warnings": []}]},
        "facts": {"groups": [{"category": None, "facts": [{"value": 0}, {"value": False}]},
                             {"facts": [{"value": None}]}],
                  "cap_table": {"events": [], "snapshot": None}},
        "working_memory": {"items": [{"runtime": {"nested": [1, "原样", None]}}], "warnings": []},
        "extra": {"unknown": [False, {}, [], ""]},
    }
    calls = []

    def read(*, company_id):
        calls.append(company_id)
        return payload

    monkeypatch.setattr(push, "knowledge_service", lambda: SimpleNamespace(read=read))
    return payload, calls


@contextmanager
def cloud(monkeypatch, *, status=200, response=b'{"ok": true}', location=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), body))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SAM_KNOWLEDGE_CLOUD_URL", f"http://127.0.0.1:{server.server_port}/")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("missing", ["SAM_COMPANY_ID", "SAM_KNOWLEDGE_CLOUD_URL",
                                     "SAM_KNOWLEDGE_INGEST_KEY"])
def test_missing_settings_before_app_import(missing):
    env = dict(os.environ)
    for name in ("SAM_COMPANY_ID", "SAM_KNOWLEDGE_CLOUD_URL", "SAM_KNOWLEDGE_INGEST_KEY"):
        env[name] = "acc-placeholder"
    env.pop(missing)
    env.pop("SAM_MEMORY_ROOT_URI", None)
    script = Path(push.__file__).resolve()
    result = subprocess.run([sys.executable, "-S", str(script)], env=env, cwd=script.parent,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert missing in result.stderr
    if missing == "SAM_COMPANY_ID":
        assert "usage:" in result.stderr
        assert "--company-id" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("argv,company", [([], "acc-push"), (["--company-id", "acc-override"], "acc-override")])
def test_snapshot_post_is_unchanged(push_env, monkeypatch, capsys, argv, company):
    payload, calls = push_env
    original = deepcopy(payload)
    before = time.time()
    with cloud(monkeypatch) as requests:
        assert push.main(argv) == 0
    path, headers, raw = requests[0]
    sent = json.loads(raw)
    assert path == "/ingest"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Ingest-Key"] == "acc-test-key"
    assert set(sent) == {"company_id", "pushed_at", "payload"}
    assert sent["company_id"] == company
    assert isinstance(sent["pushed_at"], float)
    assert before <= sent["pushed_at"] <= time.time()
    assert sent["payload"] == original == payload
    assert "中文原文".encode("utf-8") in raw
    assert calls == [company]
    output = capsys.readouterr()
    assert "succeeded" in output.out
    assert f"company_id={company} host=127.0.0.1" in output.out
    assert "files=2 facts=3 working_memory=1" in output.out
    assert "acc-test-key" not in output.out + output.err


@pytest.mark.parametrize("status", [201, 401, 500])
def test_http_failure(push_env, monkeypatch, capsys, status):
    with cloud(monkeypatch, status=status, response=b'{"error": "denied"}'):
        assert push.main([]) == 1
    output = capsys.readouterr()
    assert f"HTTP {status}" in output.err
    assert '{"error": "denied"}' in output.err
    assert not output.out


@pytest.mark.parametrize("response", [b'{"ok":false}', b'{"ok":1}', b'[]', b'not JSON'])
def test_200_requires_json_true(push_env, monkeypatch, capsys, response):
    with cloud(monkeypatch, response=response):
        assert push.main([]) == 1
    assert "HTTP 200" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [urllib.error.URLError("acc-test-key transport failed"),
                                     TimeoutError("acc-test-key transport failed"),
                                     http.client.IncompleteRead(b"", 5)])
def test_network_failure_does_not_log_key(push_env, monkeypatch, capsys, failure):
    monkeypatch.setenv("SAM_KNOWLEDGE_CLOUD_URL", "http://127.0.0.1")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(push.urllib.request, "build_opener", fail)
    assert push.main([]) == 1
    output = capsys.readouterr()
    assert "Knowledge push failed" in output.err
    assert "acc-test-key" not in output.err + output.out


def test_read_failure_does_not_send(push_env, monkeypatch, capsys):
    monkeypatch.setenv("SAM_KNOWLEDGE_CLOUD_URL", "http://127.0.0.1")

    def fail():
        raise RuntimeError("Missing SAM_MEMORY_ROOT_URI")

    monkeypatch.setattr(push, "knowledge_service", fail)
    monkeypatch.setattr(push.urllib.request, "build_opener", lambda *args: pytest.fail("must not send"))
    assert push.main([]) == 1
    assert "Missing SAM_MEMORY_ROOT_URI" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["not-a-url", "ftp://127.0.0.1", "http://127.0.0.1:bad",
                                 "http://user:secret@127.0.0.1", "http://127.0.0.1?secret=value"])
def test_invalid_url_before_read(push_env, monkeypatch, capsys, url):
    monkeypatch.setenv("SAM_KNOWLEDGE_CLOUD_URL", url)
    assert push.main([]) == 1
    assert push_env[1] == []
    output = capsys.readouterr()
    assert "SAM_KNOWLEDGE_CLOUD_URL" in output.err
    assert "secret" not in output.err


def test_redirect_is_not_followed(push_env, monkeypatch, capsys):
    with cloud(monkeypatch) as destination:
        location = os.environ["SAM_KNOWLEDGE_CLOUD_URL"] + "ingest"
        with cloud(monkeypatch, status=307, location=location):
            assert push.main([]) == 1
        assert destination == []
    assert "HTTP 307" in capsys.readouterr().err


@pytest.fixture
def homepage_env(monkeypatch):
    """Synthetic dual-channel homepage push setup; no real cloud or credentials."""
    monkeypatch.delenv("SAM_CLOUD_URL", raising=False)
    monkeypatch.setenv("SAM_COMPANY_ID", "acc-push")
    monkeypatch.setenv("SAM_CLOUD_INGEST_KEY", "acc-home-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    home = {"version": 1, "company_id": "acc-push", "pushed_at": 1.5,
            "payload": {"page_context": {"facts": []}, "knowledge": {"evidence": {}}}}
    tree = {"version": 1, "company_id": "acc-push", "pushed_at": 1.5,
            "payload": {"facts_tree": {"company_id": "acc-push", "state": "ready",
                                       "tree": {"brief": "树简报原文"}}}}
    monkeypatch.setattr(push, "home_snapshot", lambda company_id: deepcopy(home))
    monkeypatch.setattr(push, "facts_tree_snapshot", lambda company_id: deepcopy(tree))
    return home, tree


@contextmanager
def cloud_leg_failure(monkeypatch, *, status=500, response=b'{"error": "denied acc-home-key"}'):
    """Two-leg cloud stub: /ingest succeeds, /ingest/facts-tree fails."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), body))
            if self.path == "/ingest":
                code, payload = 200, b'{"ok": true}'
            else:
                code, payload = status, response
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SAM_KNOWLEDGE_CLOUD_URL", f"http://127.0.0.1:{server.server_port}/")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_homepage_dual_push_when_gate_open(homepage_env, monkeypatch, capsys):
    from app import settings
    home, tree = homepage_env
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)
    with cloud(monkeypatch) as requests:
        monkeypatch.setenv("SAM_CLOUD_URL", os.environ["SAM_KNOWLEDGE_CLOUD_URL"])
        assert push.main(["--homepage"]) == 0
    assert [path for path, _, _ in requests] == ["/ingest", "/ingest/facts-tree"]
    home_body = json.loads(requests[0][2])
    tree_body = json.loads(requests[1][2])
    assert home_body == home and "facts_tree" not in home_body["payload"]
    assert tree_body == tree and set(tree_body["payload"]) == {"facts_tree"}
    for _, headers, _ in requests:
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Ingest-Key"] == "acc-home-key"
    output = capsys.readouterr()
    assert "succeeded" in output.out
    assert "acc-home-key" not in output.out + output.err


def test_homepage_single_push_when_gate_closed(homepage_env, monkeypatch, capsys):
    from app import settings
    home, _ = homepage_env
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", False)
    with cloud(monkeypatch) as requests:
        monkeypatch.setenv("SAM_CLOUD_URL", os.environ["SAM_KNOWLEDGE_CLOUD_URL"])
        assert push.main(["--homepage"]) == 0
    assert [path for path, _, _ in requests] == ["/ingest"]
    assert json.loads(requests[0][2]) == home
    output = capsys.readouterr()
    assert "succeeded" in output.out
    assert "acc-home-key" not in output.out + output.err


def test_homepage_facts_tree_failure_reports_partial(homepage_env, monkeypatch, capsys):
    from app import settings
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)
    with cloud_leg_failure(monkeypatch) as requests:
        monkeypatch.setenv("SAM_CLOUD_URL", os.environ["SAM_KNOWLEDGE_CLOUD_URL"])
        assert push.main(["--homepage"]) == 1
    assert [path for path, _, _ in requests] == ["/ingest", "/ingest/facts-tree"]
    output = capsys.readouterr()
    assert "HTTP 500" in output.err
    assert "Partial push" in output.err
    assert "homepage (/ingest)" in output.err and "facts-tree (/ingest/facts-tree)" in output.err
    assert not output.out
    assert "acc-home-key" not in output.out + output.err


def test_homepage_facts_tree_build_failure_sends_nothing(homepage_env, monkeypatch, capsys):
    from app import settings
    monkeypatch.setattr(settings, "SAM_FACTS_TREE", True)

    def fail(company_id):
        raise RuntimeError("tree read failed acc-home-key")

    monkeypatch.setattr(push, "facts_tree_snapshot", fail)
    with cloud(monkeypatch) as requests:
        monkeypatch.setenv("SAM_CLOUD_URL", os.environ["SAM_KNOWLEDGE_CLOUD_URL"])
        assert push.main(["--homepage"]) == 1
    assert requests == []  # both snapshots build before any network send
    output = capsys.readouterr()
    assert "acc-home-key" not in output.out + output.err


def test_composition_reuses_runtime_config_and_provider(monkeypatch):
    from app import knowledge, providers, runtime_config

    config = SimpleNamespace(main_db=object(), objects_dir=object())
    memory = object()
    calls = []

    def from_env(*, project_root):
        assert project_root == push.PROJECT_ROOT
        return config

    def provider(actual):
        assert actual is config
        return memory

    def service(db, actual_memory, *, objects_path):
        calls.append((db, actual_memory, objects_path))
        return "service"

    monkeypatch.setattr(runtime_config.RuntimeConfig, "from_env", from_env)
    monkeypatch.setattr(providers, "memory_provider", provider)
    monkeypatch.setattr(knowledge, "KnowledgeService", service)
    assert push.knowledge_service() == "service"
    assert calls == [(config.main_db, memory, config.objects_dir)]
