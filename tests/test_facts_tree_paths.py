"""Existing address snapshots must not change when tree paths are added."""
from dataclasses import asdict

import pytest

from app.memory import paths
from app.module_tree import MODULES
from app.facts_tree.template_packs import load_pack
from app.facts_tree.uris import leaf_uri as pack_leaf_uri, tree_doc_uri, tree_prefix, trunk_doc_uri


@pytest.mark.parametrize("uri,area,company,module,parts", [
    ("sam://facts/acc-a/", "facts", "acc-a", "", ("acc-a",)),
    ("sam://facts/acc-a/info", "facts", "acc-a", "info", ("acc-a", "info")),
    ("sam://facts/acc-a/info/brief.md", "facts", "acc-a", "info", ("acc-a", "info", "brief.md")),
    ("sam://facts/acc-a/info/guide.json", "facts", "acc-a", "info", ("acc-a", "info", "guide.json")),
    ("sam://facts/acc-a/brief.md", "facts", "acc-a", "", ("acc-a", "brief.md")),
    ("sam://facts/acc-a/guide.json", "facts", "acc-a", "", ("acc-a", "guide.json")),
    ("sam://facts/acc-a/fin/F123.md", "facts", "acc-a", "fin", ("acc-a", "fin", "F123.md")),
    ("sam://facts/acc-a/待归类/F1.md", "facts", "acc-a", "待归类", ("acc-a", "待归类", "F1.md")),
    ("sam://original/acc-a/abc.pdf", "original", "acc-a", "", ("acc-a", "abc.pdf")),
    ("sam://original/acc-a/doc/body.md", "original", "acc-a", "doc", ("acc-a", "doc", "body.md")),
    ("sam://kv/co/acc-a/cap_table/events.jsonl", "kv", "acc-a", "", ("co", "acc-a", "cap_table", "events.jsonl")),
    ("sam://kv/co/acc-a/conversations/thread_summary.json", "kv", "acc-a", "", ("co", "acc-a", "conversations", "thread_summary.json")),
    ("sam://kv/rt/session/task/state.json", "kv", "", "", ("rt", "session", "task", "state.json")),
])
def test_existing_uri_snapshots(uri, area, company, module, parts):
    assert asdict(paths.parse(uri, company_id="acc-a")) == dict(
        uri=uri.removesuffix("/"), area=area, company_id=company, module_key=module, parts=parts)


@pytest.mark.parametrize("module", MODULES)
def test_leaf_and_trunk_paths(module):
    uri = paths.leaf_uri("acc-a", module.directory, f"L-{module.key}-01-测试")
    assert uri == f"sam://facts/acc-a/{module.directory}/leaves/L-{module.key}-01-测试.md"
    assert paths.leaf_uri("acc-a", module.directory, f"L-{module.key}-01-测试.md") == uri
    address = paths.parse(uri, company_id="acc-a")
    assert (address.company_id, address.module_key) == ("acc-a", module.directory)
    for doc in ("trunk-brief.md", "trunk-guide.md"):
        uri = paths.trunk_doc_uri("acc-a", module.directory, doc)
        assert paths.parse(uri).module_key == module.directory


def test_tree_meta_is_root_not_module():
    uri = paths.tree_meta_uri("acc-a")
    assert uri == "sam://facts/acc-a/tree.meta"
    assert asdict(paths.parse(uri, company_id="acc-a")) == dict(
        uri=uri, area="facts", company_id="acc-a", module_key="", parts=("acc-a", "tree.meta"))


@pytest.mark.parametrize("name", ["../bad", "_bad.md", "x/y.md", "", "a%20.md", ".hidden.md", "x" * 201])
def test_unsafe_leaf_names_rejected(name):
    with pytest.raises(ValueError): paths.leaf_uri("acc-a", MODULES[0].directory, name)


@pytest.mark.parametrize("directory", ["info", "待归类", "11-新枝", "../info", None])
def test_only_existing_trunk_directories(directory):
    with pytest.raises(ValueError): paths.leaf_uri("acc-a", directory, "test.md")
    with pytest.raises(ValueError): paths.trunk_doc_uri("acc-a", directory, "trunk-guide.md")


def test_bad_doc_and_company_scope():
    with pytest.raises(ValueError): paths.trunk_doc_uri("acc-a", MODULES[0].directory, "guide.md")
    with pytest.raises(ValueError): paths.tree_meta_uri("bad_company")
    with pytest.raises(ValueError): paths.parse(paths.tree_meta_uri("acc-a"), company_id="acc-b")


def test_unreleased_pack_uses_company_pack_version_namespace():
    pack = load_pack("generic-v2")
    leaf = pack.leaf_for("L-finance-01")
    uri = pack_leaf_uri("acc-a", pack, leaf)
    assert uri.startswith("sam://facts/co/acc-a/tree/generic-v2/1/leaves/L-finance-01-")
    address = paths.parse(uri, company_id="acc-a")
    assert address.company_id == "acc-a" and address.module_key == ""
    assert address.parts[:6] == ("co", "acc-a", "tree", "generic-v2", "1", "leaves")
    assert tree_prefix("acc-a") == "sam://facts/co/acc-a/tree/"
    assert paths.parse(tree_prefix("acc-a"), company_id="acc-a").company_id == "acc-a"
    assert trunk_doc_uri("acc-a", pack, "finance", "trunk-brief.md") == (
        "sam://facts/co/acc-a/tree/generic-v2/1/trunks/finance/trunk-brief.md")
    assert tree_doc_uri("acc-a", pack, "tree-guide.md") == (
        "sam://facts/co/acc-a/tree/generic-v2/1/tree-guide.md")
    with pytest.raises(ValueError):
        paths.parse(uri, company_id="acc-b")


def test_original_namespace_does_not_use_facts_pack_routing():
    address = paths.parse("sam://original/co/acme/tree/generic-v2/1/note.md")
    assert (address.area, address.company_id, address.module_key) == ("original", "co", "acme")
