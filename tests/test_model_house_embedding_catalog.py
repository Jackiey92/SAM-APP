"""Stage 1: controlled catalog and pure selection, without loading weights."""
import builtins
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import socket

import pytest

from app.memory.embedding import MODEL, REVISION
from app.model_house import EmbeddingCatalogError, load_embedding_catalog, resolve_embedding_spec


CATALOG_PATH = Path(__file__).resolve().parents[1] / 'app/model_house/embeddings.json'
ENTRY_KEYS = set('resource_id display_name provider_kind modality status is_default connection capabilities limits pricing evidence'.split())
SUB_KEYS = {
    'connection': set('base_url api_key_env model_id protocol'.split()),
    'capabilities': set('dim_default dims_allowed context_tokens sparse input_modes text_type_param'.split()),
    'limits': {'rpm', 'tpm'},
    'pricing': {'currency', 'per_million_input', 'per_million_image'},
    'evidence': {'source_url', 'reviewed_at', 'notes'},
}


@pytest.fixture
def raw():
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


def catalog_from(tmp_path, raw):
    path = tmp_path / 'embeddings.json'
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    return load_embedding_catalog(path)


def test_shipped_catalog(raw):
    assert set(raw) == {'schema_version', 'default_resource_id', 'resources'}
    catalog = load_embedding_catalog()
    assert catalog.schema_version == 1
    assert catalog.default_resource_id == 'local-bge'
    assert len(catalog.resources) == 2
    assert {e.resource_id for e in catalog.resources} == {'local-bge', 'qwen3.7-text'}
    assert [(e.resource_id, e.status) for e in catalog.resources if e.is_default] == [('local-bge', 'enabled')]
    assert catalog.resources[1].status == 'pending_verification'
    for entry in raw['resources']:
        assert set(entry) == ENTRY_KEYS
        for name, keys in SUB_KEYS.items():
            assert set(entry[name]) == keys
    local = catalog.resources[0]
    assert REVISION == '7999e1d3359715c523056ef9478215996d62a620'
    assert local.connection.model_id == f'{MODEL}@{REVISION}'
    assert local.capabilities.dim_default == 512


def test_deeply_immutable():
    catalog = load_embedding_catalog()
    with pytest.raises(FrozenInstanceError):
        catalog.default_resource_id = 'other'
    with pytest.raises(FrozenInstanceError):
        catalog.resources[0].connection._items = ()
    with pytest.raises(TypeError):
        catalog.resources[0].capabilities.dims_allowed[0] = 1


@pytest.mark.parametrize('requested', [None, '', '   ', 'local-bge', ' local-bge '])
def test_local_spec_no_key_no_torch_no_network(monkeypatch, requested):
    class Env(dict):
        def get(self, name, *args):
            assert name == 'SAM_EMBEDDING_MODEL', 'local selection read a credential'
            return super().get(name, *args)

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name.split('.')[0] not in {'torch', 'transformers', 'huggingface_hub'}
        return original_import(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        pytest.fail('pure selection attempted network access')

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    env = Env() if requested is None else Env(SAM_EMBEDDING_MODEL=requested)
    spec = resolve_embedding_spec(env=env)
    assert (spec.embedder, spec.model_version, spec.dim) == ('local-bge', f'{MODEL}@{REVISION}', 512)
    assert spec.provider_kind == 'local-bge'
    assert spec.base_url is None and spec.api_key is None


def test_spec_uses_environ(monkeypatch):
    monkeypatch.delenv('SAM_EMBEDDING_MODEL', raising=False)
    assert resolve_embedding_spec().resource_id == 'local-bge'


@pytest.mark.parametrize('case', [
    'schema', 'schema-bool', 'empty', 'duplicate-id', 'zero-default', 'two-defaults',
    'disabled-default', 'wrong-default-id', 'status', 'provider', 'modality',
    'connection-empty-url', 'connection-empty-model', 'connection-empty-env',
    'protocol', 'local-protocol', 'dim-default', 'dims-empty', 'dims-bool',
    'dims-string', 'entry-missing', 'entry-extra', 'top-missing', 'top-extra',
    'input-modes', 'text-type', 'default-bool', 'limits-type', 'price-type',
])
def test_bad_catalog_matrix(tmp_path, raw, case):
    local, remote = raw['resources']
    if case == 'schema': raw['schema_version'] = 2
    elif case == 'schema-bool': raw['schema_version'] = True
    elif case == 'empty': raw['resources'] = []
    elif case == 'duplicate-id': remote['resource_id'] = local['resource_id']
    elif case == 'zero-default': local['is_default'] = False
    elif case == 'two-defaults': remote.update(is_default=True, status='enabled')
    elif case == 'disabled-default': local['status'] = 'suspended'
    elif case == 'wrong-default-id': raw['default_resource_id'] = remote['resource_id']
    elif case == 'status': remote['status'] = 'unknown'
    elif case == 'provider': remote['provider_kind'] = 'unknown'
    elif case == 'modality': remote['modality'] = ['image']
    elif case.startswith('connection-empty-'):
        key = {'url': 'base_url', 'model': 'model_id', 'env': 'api_key_env'}[case.rsplit('-', 1)[1]]
        remote['connection'][key] = ' '
    elif case == 'protocol': remote['connection']['protocol'] = 'local'
    elif case == 'local-protocol': local['connection']['protocol'] = 'openai-embeddings-v1'
    elif case == 'dim-default': remote['capabilities']['dim_default'] = 3
    elif case == 'dims-empty': remote['capabilities']['dims_allowed'] = []
    elif case == 'dims-bool': remote['capabilities']['dims_allowed'] = [True]
    elif case == 'dims-string': remote['capabilities']['dims_allowed'] = ['1024']
    elif case == 'entry-missing': remote.pop('display_name')
    elif case == 'entry-extra': remote['extra'] = 'untrusted'
    elif case == 'top-missing': raw.pop('schema_version')
    elif case == 'top-extra': raw['extra'] = 'untrusted'
    elif case == 'input-modes': remote['capabilities']['input_modes'] = ['image']
    elif case == 'text-type': remote['capabilities']['text_type_param'] = True
    elif case == 'default-bool': local['is_default'] = 1
    elif case == 'limits-type': remote['limits']['rpm'] = '24000'
    elif case == 'price-type': remote['pricing']['per_million_input'] = '0.5'
    with pytest.raises(EmbeddingCatalogError):
        catalog_from(tmp_path, raw)


@pytest.mark.parametrize('name', SUB_KEYS)
@pytest.mark.parametrize('change', ['missing', 'extra'])
def test_subobject_keys_strict(tmp_path, raw, name, change):
    obj = raw['resources'][1][name]
    if change == 'missing':
        obj.pop(next(iter(obj)))
    else:
        obj['extra'] = None
    with pytest.raises(EmbeddingCatalogError):
        catalog_from(tmp_path, raw)


@pytest.mark.parametrize('body', ['{', '[]', 'null', '{"schema_version":1,"schema_version":1}', '\udcff'])
def test_unreadable_or_invalid_json(tmp_path, body):
    path = tmp_path / 'bad.json'
    path.write_bytes(body.encode('utf-8', errors='surrogateescape'))
    with pytest.raises(EmbeddingCatalogError):
        load_embedding_catalog(path)


def test_missing_file(tmp_path):
    with pytest.raises(EmbeddingCatalogError):
        load_embedding_catalog(tmp_path / 'missing.json')


@pytest.mark.parametrize('status', ['registered', 'pending_verification', 'suspended', 'quarantined', 'retired'])
def test_non_enabled_never_falls_back(tmp_path, raw, status):
    raw['resources'][1]['status'] = status
    catalog = catalog_from(tmp_path, raw)
    dummy = 'test-only-credential'
    with pytest.raises(EmbeddingCatalogError) as error:
        resolve_embedding_spec(catalog=catalog, env={'SAM_EMBEDDING_MODEL': 'qwen3.7-text', 'SAM_EMBEDDING_API_KEY': dummy})
    message = str(error.value)
    assert 'qwen3.7-text' in message and status in message
    assert '需在 embeddings.json 经宿主受控改为 enabled' in message
    assert dummy not in message


@pytest.mark.parametrize('key', [None, '', '   '])
def test_enabled_missing_key(tmp_path, raw, key, caplog):
    raw['resources'][1]['status'] = 'enabled'
    env = {'SAM_EMBEDDING_MODEL': 'qwen3.7-text', 'OTHER_KEY': 'test-only-unrelated-secret'}
    if key is not None:
        env['SAM_EMBEDDING_API_KEY'] = key
    with pytest.raises(EmbeddingCatalogError) as error:
        resolve_embedding_spec(catalog=catalog_from(tmp_path, raw), env=env)
    assert str(error.value) == 'embedding 凭据未配置：请设置环境变量 SAM_EMBEDDING_API_KEY'
    assert env['OTHER_KEY'] not in str(error.value) + caplog.text


def test_enabled_remote_spec(tmp_path, raw, caplog):
    raw['resources'][1]['status'] = 'enabled'
    dummy = 'test-only-credential'
    spec = resolve_embedding_spec(catalog=catalog_from(tmp_path, raw), env={
        'SAM_EMBEDDING_MODEL': 'qwen3.7-text', 'SAM_EMBEDDING_API_KEY': f' {dummy} ',
    })
    assert (spec.embedder, spec.model_version, spec.dim) == ('qwen3.7-text', 'qwen3.7-text-embedding', 1024)
    assert spec.provider_kind == 'openai-compatible' and spec.status == 'enabled'
    assert spec.base_url == 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    assert spec.api_key == dummy
    assert dummy not in repr(spec) + caplog.text
    with pytest.raises(FrozenInstanceError):
        spec.dim = 512


def test_unknown_resource():
    with pytest.raises(EmbeddingCatalogError):
        resolve_embedding_spec(env={'SAM_EMBEDDING_MODEL': 'missing'})


def test_invalid_catalog_does_not_echo_contents(tmp_path, raw):
    dummy = 'test-only-secret-in-corrupt-input'
    raw[dummy] = dummy
    with pytest.raises(EmbeddingCatalogError) as error:
        catalog_from(tmp_path, raw)
    assert dummy not in str(error.value)
