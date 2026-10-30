"""Sample-registration mechanics; all probe schemas are synthetic and temporary."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import shutil
import subprocess
import sys
from types import MappingProxyType

import pytest

from app.facts_tree import git_format, record_schema
from app.facts_tree.record_schema import _registry
from app.facts_tree.record_schema._registry import R1_CLOSURE_FAMILIES
from tests.test_facts_tree_record_schema_closure import STRUCTURAL_RESULT_TARGETS


COMMON = {"schema_version", "format_version", "record_type", "repo_id", "company_id"}
SCOPE = {"repo_id": "repo-test", "company_id": "company-test"}


@pytest.fixture
def registry():
    # Execute an independent registry without mutating the discovered package.
    spec = spec_from_file_location("sample_registry_probe", _registry.__file__)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample_registry(registry):
    @registry.register_schema("probe", fields=COMMON | {"entity_uid", "revision", "event_id"})
    def validator(payload, family, path, oid_length):
        pass

    @registry.register_sample("probe")
    def builder(*, entity_uid="entity-1", revision=1, event_id="event-1"):
        return {"schema_version": 1, "format_version": git_format.FORMAT_VERSION,
                "record_type": "probe", **SCOPE, "entity_uid": entity_uid,
                "revision": revision, "event_id": event_id}

    registry._freeze_registries()
    return registry


def test_builder_returns_payload_dict_without_path(sample_registry):
    payload = sample_registry.get_sample_builder("probe")()
    assert type(payload) is dict
    assert COMMON | {"entity_uid", "revision", "event_id"} == set(payload)
    assert "path" not in payload


def test_builder_returns_fresh_payload(sample_registry):
    builder = sample_registry.get_sample_builder("probe")
    first = builder()
    first["entity_uid"] = "changed"
    assert builder()["entity_uid"] == "entity-1"


@pytest.mark.parametrize("field,value", [
    ("entity_uid", "entity/二"), ("revision", 7), ("event_id", "event/二"),
])
def test_path_identity_keyword_overrides(sample_registry, field, value):
    payload = sample_registry.get_sample_builder("probe")(**{field: value})
    assert payload[field] == value
    assert COMMON <= set(payload)


def test_only_path_identity_keywords_are_accepted(sample_registry):
    with pytest.raises(TypeError):
        sample_registry.get_sample_builder("probe")(company_id="foreign")


def test_zero_argument_builder_is_preserved(registry):
    registry.register_schema("probe", fields=COMMON)(lambda *args: None)
    builder = lambda: {}
    assert registry.register_sample("probe")(builder) is builder
    registry._freeze_registries()
    assert registry.get_sample_builder("probe") is builder


def test_duplicate_sample_is_rejected_without_replacement(sample_registry):
    original = sample_registry.get_sample_builder("probe")
    with pytest.raises(ValueError, match="duplicate sample registration"):
        sample_registry.register_sample("probe")(lambda: {})
    assert sample_registry.get_sample_builder("probe") is original


def test_sample_without_schema_is_rejected(registry):
    with pytest.raises(ValueError, match="sample without schema registration"):
        registry.register_sample("unregistered")(lambda: {})
    assert registry._freeze_registries()[3] == {}


def test_noncallable_sample_is_rejected(registry):
    registry.register_schema("probe", fields=COMMON)(lambda *args: None)
    with pytest.raises(TypeError, match="sample builder must be callable"):
        registry.register_sample("probe")({})
    assert registry._freeze_registries()[3] == {}


def test_schema_without_sample_is_allowed(registry):
    registry.register_schema("probe", fields=COMMON)(lambda *args: None)
    validators, closures, fields, builders = registry._freeze_registries()
    assert set(validators) == set(fields) == {"probe"}
    assert builders == closures == {}


def test_accessor_missing_default(sample_registry):
    sentinel = object()
    assert sample_registry.get_sample_builder("missing") is None
    assert sample_registry.get_sample_builder("missing", sentinel) is sentinel


@pytest.mark.parametrize("index", range(4))
def test_discovered_registries_are_immutable(sample_registry, index):
    registry = sample_registry._freeze_registries()[index]
    assert isinstance(registry, MappingProxyType)
    with pytest.raises(TypeError):
        registry["new"] = object()
    with pytest.raises(TypeError):
        del registry["probe"]


def test_freezing_publishes_a_snapshot(sample_registry):
    snapshot = sample_registry.SCHEMA_BUILDERS
    sample_registry.register_schema("later", fields=COMMON)(lambda *args: None)
    sample_registry.register_sample("later")(lambda: {})
    assert "later" not in snapshot
    assert sample_registry.get_sample_builder("later") is None
    sample_registry._freeze_registries()
    assert "later" in sample_registry.SCHEMA_BUILDERS
    assert "later" not in snapshot


def test_discovery_invariant_and_public_facade():
    from app.facts_tree import record_schema_closure as facade

    assert set(record_schema.SCHEMA_BUILDERS) <= set(record_schema.SCHEMA_FIELDS)
    assert facade.SCHEMA_BUILDERS is record_schema.SCHEMA_BUILDERS
    assert facade.get_sample_builder is _registry.get_sample_builder
    assert facade.register_sample is _registry.register_sample
    assert isinstance(record_schema.SCHEMA_FIELDS, MappingProxyType)
    assert isinstance(record_schema.SCHEMA_BUILDERS, MappingProxyType)


@pytest.mark.parametrize("ids", [
    {"entity_uid": "entity-1", "revision": 1},
    {"entity_uid": "entity/二", "revision": 9},
])
def test_sample_passes_real_record_validator(registry, monkeypatch, ids):
    family = "entity_revision"
    calls = []

    @registry.register_schema(family, fields=COMMON | set(ids))
    def validator(payload, actual_family, path, oid_length):
        calls.append((actual_family, path, oid_length))

    @registry.register_sample(family)
    def builder(**overrides):
        return {"schema_version": 1, "format_version": git_format.FORMAT_VERSION,
                "record_type": family, **SCOPE,
                "entity_uid": "entity-1", "revision": 1, **overrides}

    validators, _, fields, builders = registry._freeze_registries()
    monkeypatch.setattr(record_schema, "SCHEMA_VALIDATORS", validators)
    monkeypatch.setattr(record_schema, "SCHEMA_FIELDS", fields)
    payload = builders[family](**ids)
    path = git_format.record_path(family, **ids)
    assert record_schema.validate_record_schema(path, payload, **SCOPE) is None
    assert calls == [(family, path, 40)]
    payload["entity_uid"] = "wrong-path"
    with pytest.raises(record_schema.RecordSchemaClosureError, match="path_identity"):
        record_schema.validate_record_schema(path, payload, **SCOPE)


def probe_plugin(family, ids):
    # probe_token deliberately has no product meaning. Requiring it proves that
    # closure-5 validates the builder's payload rather than the old thin shell.
    return f'''
from app.facts_tree import git_format
from app.facts_tree.record_schema._registry import register_schema, register_sample
from app.facts_tree.record_schema._primitives import _fail

FAMILY = {family!r}
IDS = {ids!r}
FIELDS = frozenset({COMMON!r}) | set(IDS) | {{"probe_token"}}
VALIDATED = []

@register_schema(FAMILY, fields=FIELDS)
def validate(payload, family, path, oid_length):
    VALIDATED.append(path)
    if payload["probe_token"] != "sample-was-used":
        _fail(family, path, "probe_token", "probe_token")

@register_sample(FAMILY)
def build(**overrides):
    return {{"schema_version": 1, "format_version": git_format.FORMAT_VERSION,
            "record_type": FAMILY, "repo_id": "plugin-repo", "company_id": "plugin-company",
            **IDS, **overrides, "probe_token": "sample-was-used"}}
'''


def run_discovery_probe(tmp_path, source, body):
    # Keep probes independent of business plugins delivered by later cards.
    modules = {"__init__", "_registry", "_primitives"}
    validators = [record_schema.SCHEMA_VALIDATORS[f] for f in R1_CLOSURE_FAMILIES]
    validators += [record_schema.CLOSURE_VALIDATORS[n] for n in (1, 5)]
    modules.update(v.__module__.rsplit(".", 1)[1] for v in validators)
    package = tmp_path / "record_schema"
    package.mkdir()
    for module in sorted(modules):
        filename = module + ".py"
        shutil.copyfile(Path(record_schema.__file__).parent / filename, package / filename)
    plugin = package / "sample_probe.py"
    plugin.write_text(source, encoding="utf-8")
    script = f'''
import app.facts_tree
app.facts_tree.__path__.insert(0, {str(tmp_path)!r})
from app.facts_tree import record_schema
{body}
'''
    try:
        return subprocess.run([sys.executable, "-B", "-c", script],
                              capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(package)


def test_discovery_rejects_orphan_sample_even_at_freeze(tmp_path):
    result = run_discovery_probe(tmp_path, '''
from app.facts_tree.record_schema import _registry
_registry._sample_builders["orphan"] = lambda: {}
''', "")
    assert result.returncode != 0
    assert "AssertionError: sample registered without schema" in result.stderr
    assert not (tmp_path / "record_schema").exists()


@pytest.mark.parametrize("family", tuple(dict.fromkeys(f for f, _ in STRUCTURAL_RESULT_TARGETS)))
def test_temporary_plugin_automatically_enables_closure5_target(tmp_path, family):
    from tests.test_facts_tree_record_schema_closure import synthetic_tree

    original_builders = dict(record_schema.SCHEMA_BUILDERS)
    tree, paths = synthetic_tree(family)
    ids = git_format._match_template(family, paths[4])
    enabled = sum(f == family for f, _ in STRUCTURAL_RESULT_TARGETS)
    body = '''
import pytest
exit_code = pytest.main([
    "-q", "-rs", "tests/test_facts_tree_record_schema_closure.py",
])
raise SystemExit(exit_code)
'''
    result = run_discovery_probe(tmp_path, probe_plugin(family, ids), body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{220 + enabled} passed" in result.stdout
    assert f"{len(STRUCTURAL_RESULT_TARGETS) - enabled} skipped" in result.stdout
    assert f"{family} family sample pending" not in result.stdout
    assert not (tmp_path / "record_schema").exists()
    assert dict(record_schema.SCHEMA_BUILDERS) == original_builders


def test_all_business_schemas_leave_entire_r1_suite_independent(tmp_path):
    # Derive path identities from the path authority, not an appendix field table.
    # Frozen wrappers and the frozen binding format are not new business plugins.
    families = set(git_format.RECORD_TYPES) - set(R1_CLOSURE_FAMILIES) - {
        "repository_meta", "taxonomy_manifest", "taxonomy_leaf", "bindings_current"}
    source = ["PROBES = []"]
    for family in sorted(families):
        ids = {name: 1 if name in git_format._REVISION_NAMES else "probe-id"
               for name in git_format._TEMPLATES[family][1]}
        source += ["namespace = {}", f"exec({probe_plugin(family, ids)!r}, namespace)",
                   "PROBES.append(namespace)"]
    result = run_discovery_probe(tmp_path, "\n".join(source), '''
import pytest
exit_code = pytest.main(["-q", "-rs", "tests/test_facts_tree_record_schema_closure.py"])
targets = {f for f, _ in __import__(
    "tests.test_facts_tree_record_schema_closure", fromlist=["STRUCTURAL_RESULT_TARGETS"]
).STRUCTURAL_RESULT_TARGETS}
raise SystemExit(exit_code)
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "227 passed" in result.stdout
    assert "skipped" not in result.stdout
    assert not (tmp_path / "record_schema").exists()


@pytest.mark.parametrize("family", ["fact_revision", "evidence"])
def test_mechanism_uses_real_closures_without_business_schema(monkeypatch, family):
    from tests.test_facts_tree_record_schema_closure import (
        SCOPE, mechanism_tree, validate_mechanism,
    )

    tree, paths = mechanism_tree(family)
    output_path = paths[4]
    calls = []

    def reject_business(payload, actual_family, path, oid_length):
        raise AssertionError("business schema reached by mechanism test")

    validators = {**record_schema.SCHEMA_VALIDATORS, family: reject_business}
    fields = {**record_schema.SCHEMA_FIELDS, family: frozenset(tree[output_path])}
    monkeypatch.setattr(record_schema, "SCHEMA_VALIDATORS", validators)
    monkeypatch.setattr(record_schema, "SCHEMA_FIELDS", fields)
    for number in (1, 5):
        original = record_schema.CLOSURE_VALIDATORS[number]

        def closure(*args, number=number, original=original):
            calls.append(number)
            return original(*args)

        # The exported mapping stays immutable; only the test module's view is replaced.
        from tests import test_facts_tree_record_schema_closure as r1
        monkeypatch.setattr(r1, "CLOSURE_VALIDATORS", {**r1.CLOSURE_VALIDATORS, number: closure})

    assert validate_mechanism(tree) is None
    assert calls == [1, 5]
    with pytest.raises(AssertionError, match="business schema reached"):
        record_schema.validate_record_schema(output_path, tree[output_path], **SCOPE)


def test_mechanism_preserves_nested_reference_field_coordinates():
    from tests.test_facts_tree_record_schema_closure import mechanism_tree, rejected, typed

    tree, paths = mechanism_tree()
    tree[paths[4]]["nested"] = [{"source_ref": typed("source_version", "missing", "v1")}]
    error = rejected(tree, "unresolved_typed_ref", "fact_revision", paths[4])
    assert error.field == "$.nested[0].source_ref"
