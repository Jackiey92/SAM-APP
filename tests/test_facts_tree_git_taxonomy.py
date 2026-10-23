"""Deterministic taxonomy records built from a validated template pack.

Synthetic checks only: the in-repo ``generic`` and ``generic-v2``
template YAMLs are read through template_packs.validate_pack
(generic-v2 as an explicit draft load); no real company data, files
beyond the template registry, Git objects, databases, models or
services are touched, and the draft pack stays unreleased and unbound.
Contract under test: F-001 P01 sections 10.2.2, 10.2.3 and 14.2
(approved design baseline 36f9035555c7a016d0f51eb2a266611a36ab5e3f): a
taxonomy manifest plus one record per leaf at fixed record_path paths,
each payload carrying its own schema_version and the git_format
serializer's format_version as two independent version fields.
"""
from dataclasses import replace

import pytest

from app.facts_tree import git_format
from app.facts_tree import git_taxonomy
from app.facts_tree import template_packs
from app.facts_tree.git_taxonomy import SCHEMA_VERSION, build_taxonomy_records

PACK_VERSION = "packver-synthetic-0001"


def _v2_pack():
    # Explicit draft load for isolated tests; generic-v2 is never
    # listed by available_packs() and never bindable.
    return template_packs.validate_pack("generic-v2")


def _expected_manifest_payload(pack):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
    }
    payload.update(template_packs.manifest_document(pack))
    payload["manifest_sha256"] = pack.manifest_sha256
    return payload


def _expected_leaf_payload(pack, leaf):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "format_version": git_format.FORMAT_VERSION,
        "pack": pack.pack,
        "version": pack.version,
        "code": leaf.code,
        "trunk": leaf.trunk,
        "title": leaf.title,
        "scope": leaf.scope,
    }
    if leaf.shape is not None:
        payload["shape"] = leaf.shape
    return payload


def test_records_are_deterministic_and_ordered():
    pack = _v2_pack()
    first = build_taxonomy_records(pack, pack_version=PACK_VERSION)
    second = build_taxonomy_records(pack, pack_version=PACK_VERSION)
    assert first == second
    for (path_a, bytes_a), (path_b, bytes_b) in zip(first, second):
        assert path_a == path_b
        assert bytes_a == bytes_b
        assert isinstance(bytes_a, bytes)
    assert first[0][0] == git_format.record_path(
        "taxonomy_manifest", pack_version=PACK_VERSION)
    leaf_paths = [path for path, _ in first[1:]]
    assert leaf_paths == [
        git_format.record_path(
            "taxonomy_leaf", pack_version=PACK_VERSION, leaf_id=leaf.code)
        for leaf in pack.leaves
    ]


def test_generic_v2_records_match_pack_definition():
    pack = _v2_pack()
    records = build_taxonomy_records(pack, pack_version=PACK_VERSION)
    document = template_packs.manifest_document(pack)
    assert len(document["trunks"]) == 9
    assert len(pack.leaves) == 60
    assert len(records) == 61

    manifest_path, manifest_bytes = records[0]
    assert manifest_path == "taxonomy/%s/manifest.json" % PACK_VERSION
    manifest = git_format.loads_record(manifest_bytes)
    # Canonical bytes round-trip: parsing and re-serializing must
    # reproduce the exact same bytes.
    assert git_format.dumps_record(manifest) == manifest_bytes
    # Schema and serializer versions are two independent fields, each
    # pinned to its own constant.
    assert type(manifest["schema_version"]) is int
    assert type(manifest["format_version"]) is int
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["format_version"] == git_format.FORMAT_VERSION
    # The digest closes over the validated manifest document.
    assert manifest["manifest_sha256"] == pack.manifest_sha256
    assert pack.manifest_sha256 == template_packs.manifest_digest(document)
    # Every manifest document field enters the record losslessly, array
    # declaration order included, and nothing semantic is added.
    for key, value in document.items():
        assert manifest[key] == value
    assert [t["code"] for t in manifest["trunks"]] == \
        [t["code"] for t in document["trunks"]]
    assert [l["code"] for l in manifest["leaves"]] == \
        [l["code"] for l in document["leaves"]]
    assert manifest == _expected_manifest_payload(pack)

    seen_trunks = set()
    for (path, content), leaf in zip(records[1:], pack.leaves):
        seen_trunks.add(leaf.trunk)
        assert path == git_format.record_path(
            "taxonomy_leaf", pack_version=PACK_VERSION, leaf_id=leaf.code)
        payload = git_format.loads_record(content)
        assert git_format.dumps_record(payload) == content
        # Each field is item-by-item equal to the pack definition.
        assert payload["code"] == leaf.code
        assert payload["trunk"] == leaf.trunk
        assert payload["title"] == leaf.title
        assert payload["scope"] == leaf.scope
        assert payload["pack"] == pack.pack
        assert payload["version"] == pack.version
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["format_version"] == git_format.FORMAT_VERSION
        # generic-v2 leaves carry no n/s shape (2026-10-17 裁定).
        assert leaf.shape is None
        assert "shape" not in payload
        assert payload == _expected_leaf_payload(pack, leaf)
    assert seen_trunks == {t["code"] for t in document["trunks"]}


def test_generic_pack_leaf_records_keep_shape():
    pack = template_packs.validate_pack("generic")
    records = build_taxonomy_records(pack, pack_version=PACK_VERSION)
    assert len(records) == 1 + 98
    manifest = git_format.loads_record(records[0][1])
    assert manifest == _expected_manifest_payload(pack)
    for (_, content), leaf in zip(records[1:], pack.leaves):
        payload = git_format.loads_record(content)
        assert payload["shape"] == leaf.shape
        assert payload == _expected_leaf_payload(pack, leaf)


def test_paths_come_only_from_record_path():
    pack = _v2_pack()
    # An opaque pack_version carrying traversal characters enters the
    # path only as one encode_id-encoded safe segment.
    hostile = "../evil/../../x"
    records = build_taxonomy_records(pack, pack_version=hostile)
    manifest_path = records[0][0]
    assert manifest_path == git_format.record_path(
        "taxonomy_manifest", pack_version=hostile)
    encoded = git_format.encode_id(hostile)
    assert manifest_path == "taxonomy/%s/manifest.json" % encoded
    assert git_format.decode_id(encoded) == hostile
    for path, _ in records:
        assert path.startswith("taxonomy/" + encoded + "/")
        segments = path.split("/")
        assert ".." not in segments
        assert "." not in segments
        assert "" not in segments
        assert "/../" not in path
    for (path, _), leaf in zip(records[1:], pack.leaves):
        assert path == git_format.record_path(
            "taxonomy_leaf", pack_version=hostile, leaf_id=leaf.code)


def test_fail_closed_on_bad_inputs():
    pack = _v2_pack()
    # Non-TemplatePack input.
    for bad in (None, {}, "generic-v2", pack.leaves):
        with pytest.raises(TypeError):
            build_taxonomy_records(bad, pack_version=PACK_VERSION)
    # A forged digest, a non-string digest and a tampered leaf list all
    # break the manifest digest closure.
    with pytest.raises(ValueError):
        build_taxonomy_records(
            replace(pack, manifest_sha256="0" * 64),
            pack_version=PACK_VERSION)
    with pytest.raises(ValueError):
        build_taxonomy_records(
            replace(pack, manifest_sha256=None), pack_version=PACK_VERSION)
    with pytest.raises(ValueError):
        build_taxonomy_records(
            replace(pack, leaves=pack.leaves[:-1]), pack_version=PACK_VERSION)
    # Illegal pack_version IDs: wrong types, empty, over-budget and a
    # lone surrogate that can never encode to UTF-8 (reported as a
    # ValueError, never a raw UnicodeEncodeError).
    for bad in (None, 1, b"x", 1.5):
        with pytest.raises(TypeError):
            build_taxonomy_records(pack, pack_version=bad)
    for bad in ("", "x" * (git_format.MAX_ID_CHARS + 1), "\ud800"):
        with pytest.raises(ValueError):
            build_taxonomy_records(pack, pack_version=bad)


def test_release_and_binding_gates_untouched():
    # This module never calls the released-pack loader.
    assert "load_released_pack" not in vars(git_taxonomy)
    assert template_packs.available_packs() == ["generic"]
    pack = _v2_pack()
    build_taxonomy_records(pack, pack_version=PACK_VERSION)
    # Building records for the explicit draft changes nothing: the
    # released set is unchanged and the draft still refuses release
    # loading and binding.
    assert template_packs.available_packs() == ["generic"]
    with pytest.raises(ValueError):
        template_packs.load_released_pack("generic-v2")
