"""Remote contract exercised only against a guarded loopback HTTP server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
import time

import pytest

from app.memory.embedding import EmbeddingRequestError, RemoteOpenAICompatibleProvider


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    connect = socket.socket.connect

    def guarded(sock, address):
        assert address[0] == '127.0.0.1', 'external connection forbidden'
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr('urllib.request.getproxies', lambda: {})


@pytest.fixture
def stub():
    state = {'status': 200, 'body': None, 'delay': 0, 'requests': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append((self.path, dict(self.headers), body))
            time.sleep(state['delay'])
            response = state['body']
            if response is None:
                response = {'data': [{'index': i, 'embedding': [float(i + 1)] * body['dimensions']}
                                     for i in reversed(range(len(body['input'])))]}
            payload = response if isinstance(response, bytes) else json.dumps(response).encode()
            try:
                self.send_response(state['status'])
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    state['endpoint'] = f'http://127.0.0.1:{server.server_port}/v1/'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def provider(stub, **overrides):
    args = dict(enabled=True, endpoint=stub['endpoint'], api_key='dummy-test-key', model='m', dim=8)
    args.update(overrides)
    return RemoteOpenAICompatibleProvider(**args)


def test_success_and_query(stub):
    client = provider(stub, embedder='custom')
    assert client.enabled and client.embedder == 'custom' and client.timeout == 30.0
    assert client.embed_texts(['甲', '乙']) == [[1.] * 8, [2.] * 8]
    assert client.embed_query('query') == [1.] * 8
    path, headers, body = stub['requests'][0]
    assert path == '/v1/embeddings'
    assert headers['Authorization'] == 'Bearer dummy-test-key'
    assert headers['Content-Type'] == 'application/json'
    assert body == {'model': 'm', 'input': ['甲', '乙'], 'dimensions': 8, 'encoding_format': 'float'}
    assert stub['requests'][1][2]['input'] == ['query']


@pytest.mark.parametrize('status', [400, 401, 403, 429, 500, 503])
def test_http_failures_redacted(stub, status):
    stub.update(status=status, body=b'dummy-test-key Authorization: secret')
    with pytest.raises(EmbeddingRequestError) as error:
        provider(stub).embed_query('q')
    assert 'dummy-test-key' not in str(error.value)
    assert 'Authorization' not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize('case', [
    'json', 'data', 'index', 'embedding', 'more', 'less', 'gap', 'duplicate',
    'bool-index', 'dim', 'nan', 'inf', 'zero', 'string', 'bool-number', 'not-list',
])
def test_invalid_responses(stub, case):
    rows = [{'index': 0, 'embedding': [1.] * 8}, {'index': 1, 'embedding': [2.] * 8}]
    response = {'data': rows}
    if case == 'json': response = b'not JSON dummy-test-key'
    elif case == 'data': response = {}
    elif case == 'index': rows[0].pop('index')
    elif case == 'embedding': rows[0].pop('embedding')
    elif case == 'more': rows.append({'index': 2, 'embedding': [3.] * 8})
    elif case == 'less': rows.pop()
    elif case == 'gap': rows[1]['index'] = 2
    elif case == 'duplicate': rows[1]['index'] = 0
    elif case == 'bool-index': rows[0]['index'] = False
    elif case == 'dim': rows[0]['embedding'] = [1.]
    elif case == 'nan': rows[0]['embedding'][0] = float('nan')
    elif case == 'inf': rows[0]['embedding'][0] = float('inf')
    elif case == 'zero': rows[0]['embedding'] = [0.] * 8
    elif case == 'string': rows[0]['embedding'][0] = '1'
    elif case == 'bool-number': rows[0]['embedding'][0] = True
    elif case == 'not-list': rows[0]['embedding'] = '12345678'
    stub['body'] = response
    with pytest.raises(EmbeddingRequestError) as error:
        provider(stub).embed_texts(['a', 'b'])
    assert 'dummy-test-key' not in str(error.value)


def test_closed_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with pytest.raises(EmbeddingRequestError):
        provider({'endpoint': f'http://127.0.0.1:{port}'}).embed_query('q')


def test_timeout(stub):
    stub['delay'] = 0.1
    with pytest.raises(EmbeddingRequestError):
        provider(stub, timeout=0.01).embed_query('q')


@pytest.mark.parametrize('texts', [None, 'text', ('text',), [None], [1], [''], ['  '], ['ok', '']])
def test_bad_input_no_request(stub, texts):
    with pytest.raises(ValueError):
        provider(stub).embed_texts(texts)
    assert not stub['requests']


def test_empty_no_request(stub):
    assert provider(stub).embed_texts([]) == []
    assert not stub['requests']


@pytest.mark.parametrize('overrides,message', [
    ({'enabled': False}, 'remote embedding requires explicit authorization and configuration'),
    ({'endpoint': None}, 'remote embedding requires explicit authorization and configuration'),
    ({'endpoint': ''}, 'remote embedding requires explicit authorization and configuration'),
    ({'api_key': ''}, 'remote embedding requires explicit authorization and configuration'),
    ({'model': ''}, 'remote embedding requires explicit authorization and configuration'),
    *[({'dim': v}, 'invalid embedding dimension') for v in [0, -1, True, 1.5, '8']],
    *[({'timeout': v}, 'invalid embedding timeout') for v in [0, -1, True, '1']],
])
def test_constructor_guard(overrides, message):
    with pytest.raises(ValueError, match=f'^{message}$'):
        provider({'endpoint': 'http://127.0.0.1:1'}, **overrides)
