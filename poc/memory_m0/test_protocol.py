import os
from unittest.mock import patch
import pytest
from providers import RemoteOpenAICompatibleProvider

def test_remote_default_impossible():
    with patch.dict(os.environ, {'SAM_EMBEDDING_PROVIDER':'local'}):
        with pytest.raises(ValueError, match='explicit opt-in'):
            RemoteOpenAICompatibleProvider('https://invalid.example','fake','mock')

def test_remote_configuration_required():
    with patch.dict(os.environ, {'SAM_EMBEDDING_PROVIDER':'remote'}):
        with pytest.raises(ValueError, match='incomplete'):
            RemoteOpenAICompatibleProvider('', '', '')

def test_remote_skeleton_cannot_connect():
    with patch.dict(os.environ, {'SAM_EMBEDDING_PROVIDER':'remote'}):
        provider=RemoteOpenAICompatibleProvider('https://invalid.example','fake','mock')
        with pytest.raises(NotImplementedError, match='no network implementation'):
            provider.embed_query('测试')
