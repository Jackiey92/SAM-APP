import json
import subprocess

import pytest

from app.memory import paths
from app.memory.company_facts.paths import company_uri, fact_uri, guide_md_uri
from app.memory_map import MapBuilder, source_map_uri
from app.memory.taxonomy import MODULE_KEYS


def test_logical_paths_ignore_transport_root():
    assert company_uri('acc-gudou', root='sam://unused') == 'sam://facts/acc-gudou'
    assert fact_uri('acc-gudou', 1, module_key='fin') == 'sam://facts/acc-gudou/fin/F1.md'
    assert fact_uri('acc-gudou', 1, module_key='finance') == 'sam://facts/acc-gudou/待归类/F1.md'
    assert source_map_uri('acc-gudou', 'a' * 64).startswith('sam://original/acc-gudou/')
    with pytest.raises(ValueError): paths.parse('sam://session/acc-gudou')


def test_map_has_independent_fixed_fact_tree():
    result = MapBuilder(object()).rebuild_map('acc-gudou').map
    assert {item['key'] for item in result['modules']} == MODULE_KEYS
    assert all(item['uri'].startswith('sam://facts/acc-gudou/') for item in result['modules'])
    assert result['special_directories'] == ['待办与冲突', '待归类']
