"""M4 rename only: frozen pre-change grants and canonical consumer hashes."""
import hashlib
import json
from pathlib import Path
import pytest
from app.contracts.phase0 import ROLE_ZONE_ALLOWLIST, ZONES

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
 'file_processor': {'read': {'raw_objects','runtime_outbox','l2_staging','memory_resources'},
                    'write': {'runtime_outbox','l2_staging','memory_resources'}},
 'finance_analyst': {'read': {'runtime_outbox','l2_staging','memory_resources'}, 'write': {'facts_2b'}},
 'legal_analyst': {'read': {'runtime_outbox','l2_staging','memory_resources'}, 'write': {'facts_2b'}},
 'business_analyst': {'read': {'runtime_outbox','l2_staging','memory_resources','facts_2b'}, 'write': {'facts_2b'}}
}

@pytest.mark.parametrize('role', sorted(EXPECTED))
@pytest.mark.parametrize('access', ['read', 'write'])
@pytest.mark.parametrize('zone', ['raw_objects','runtime_outbox','l2_staging','memory_resources','facts_2b','ledger_r1'])
def test_every_permission_cell_is_equivalent(role, access, zone):
    assert (zone in ROLE_ZONE_ALLOWLIST[role][access]) == (zone in EXPECTED[role][access])
    assert ZONES == {'raw_objects','runtime_outbox','l2_staging','memory_resources','facts_2b','ledger_r1'}

BASELINE_HASHES = {'harness-openclaw/config/phase2-tool-search.json': '76de0d3e84c6f48637b6f6f2b62cccaca4311f3e7942692fdcfe7f7771323c14', 'harness-openclaw/config/phase3-roles.json': '2e3626456e75d40367ef3e4521b08e91ef3619036750679b88b13d75c57b300a', 'harness-openclaw/tool-registry/manifest.json': '2e88fdb4bf25a6844772fddf67b8f8640861d5f1e67a2813b9a8257be0ccf233'}

@pytest.mark.parametrize('path', sorted(BASELINE_HASHES))
def test_permission_consumers_changed_only_zone_name(path):
    encoded = json.dumps(json.loads((ROOT / path).read_text()), sort_keys=True, separators=(',',':')).encode()
    assert hashlib.sha256(encoded).hexdigest() == BASELINE_HASHES[path]
