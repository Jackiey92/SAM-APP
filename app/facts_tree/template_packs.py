"""Read-only template registry; validates structure, never classifies facts."""
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from ..module_tree import MODULES, MODULE_KEYS
from ..memory.paths import identifier, _segment

TEMPLATES_ROOT = Path(__file__).parent / "templates"
_GENERIC_COUNTS = dict(zip((m.key for m in MODULES), (12, 15, 9, 9, 4, 18, 19, 2, 5, 5)))
# Unreleased draft packs: explicitly loadable for isolated tests, never
# listed by available_packs() and never bindable (CompanyBindingStore only
# accepts "generic"). Removal from this set is the release action.
_UNRELEASED_PACKS = frozenset({"generic-v2"})


@dataclass(frozen=True)
class LeafCode:
    code: str
    trunk: str
    title: str
    shape: str | None  # None for generic-v2: no n/s shape (2026-10-17 裁定)
    scope: str


@dataclass(frozen=True)
class TemplatePack:
    pack: str
    version: int
    is_industry_pack: bool
    trunks: tuple[Mapping, ...]
    leaves: tuple[LeafCode, ...]
    manifest_sha256: str

    def trunk_for(self, code: str):
        return next((trunk for trunk in self.trunks if trunk.get("code") == code), None)

    def leaf_for(self, code: str):
        return next((leaf for leaf in self.leaves if leaf.code == code), None)

    def trunk_directory(self, code: str) -> str:
        trunk = self.trunk_for(code)
        if trunk is None:
            raise KeyError(f"unknown trunk code: {code!r}")
        return trunk["directory"]


def _manifest_document(pack: str, version: int, is_industry_pack: bool,
                       trunks, leaves) -> dict:
    # The document covers only the validated semantic manifest: YAML comments,
    # key order and file timestamps must not change it. Declaration order of
    # the trunks/leaves lists is part of the manifest and is preserved.
    return {
        "pack": pack,
        "version": version,
        "is_industry_pack": is_industry_pack,
        "trunks": [{k: t[k] for k in sorted(t)} for t in trunks],
        # Only declared leaf keys enter the manifest: generic-v1 leaves always
        # carry all five keys (identical output), v2 leaves have no shape key.
        "leaves": [{k: leaf[k] for k in ("code", "trunk", "title", "shape", "scope")
                    if k in leaf} for leaf in leaves],
    }


def manifest_json(document) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def manifest_digest(document) -> str:
    return hashlib.sha256(manifest_json(document).encode("utf-8")).hexdigest()


def _manifest_digest(pack: str, version: int, is_industry_pack: bool,
                     trunks, leaves) -> str:
    return manifest_digest(_manifest_document(pack, version, is_industry_pack,
                                              trunks, leaves))


def manifest_document(pack: "TemplatePack") -> dict:
    """Deterministic manifest document of a validated pack, for archiving."""
    return _manifest_document(
        pack.pack, pack.version, pack.is_industry_pack,
        [dict(t) for t in pack.trunks],
        [{"code": leaf.code, "trunk": leaf.trunk, "title": leaf.title,
          **({"shape": leaf.shape} if leaf.shape is not None else {}),
          "scope": leaf.scope} for leaf in pack.leaves])


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"invalid or duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _v2_trunk_codes(trunks):
    # generic-v2 declares its own nine business trunks (P1, approved
    # 2026-10-16). The codes are a new namespace and are never checked
    # against or reinterpreted through the legacy ten MODULE_KEYS.
    codes, orders = set(), set()
    for trunk in trunks:
        if not isinstance(trunk, dict):
            raise ValueError("trunk must be a mapping")
        code = trunk.get("code")
        if not isinstance(code, str) or code in codes:
            raise ValueError("unknown or duplicate trunk")
        identifier(code)
        if (type(trunk.get("order")) is not int or trunk["order"] < 1
                or trunk["order"] in orders):
            raise ValueError("trunk order must be a unique positive integer")
        _segment(trunk.get("directory"))
        if set(trunk) != {"code", "order", "directory"}:
            raise ValueError("unknown trunk fields")
        codes.add(code)
        orders.add(trunk["order"])
    if orders != set(range(1, len(orders) + 1)):
        raise ValueError("trunk orders must be continuous from 1")
    return codes


def _v2_leaves(raw_leaves, trunk_codes):
    # v2 leaves carry no n/s shape (2026-10-17 裁定): employees choose
    # paragraphs or tables per fact; only code/trunk/title/scope are pinned.
    leaves, seen, numbers = [], set(), {code: [] for code in trunk_codes}
    for leaf in raw_leaves:
        if not isinstance(leaf, dict):
            raise ValueError("leaf must be a mapping")
        if set(leaf) != {"code", "trunk", "title", "scope"}:
            raise ValueError("invalid leaf fields")
        code = leaf["code"]
        if any(not isinstance(leaf[k], str) or (k != "scope" and not leaf[k].strip())
               for k in leaf):
            raise ValueError("leaf fields must be strings (only scope may be empty)")
        parts = code.split("-")
        if (len(parts) != 3 or parts[0] != "L" or parts[1] not in trunk_codes
                or len(parts[2]) != 2 or any(c not in "0123456789" for c in parts[2])):
            raise ValueError("invalid leaf code")
        if leaf["trunk"] != parts[1]:
            raise ValueError("leaf trunk differs from code")
        if code in seen:
            raise ValueError("duplicate leaf code")
        seen.add(code)
        numbers[parts[1]].append(int(parts[2]))
        leaves.append(LeafCode(code=code, trunk=leaf["trunk"], title=leaf["title"],
                               shape=None, scope=leaf["scope"]))
    for trunk, nums in numbers.items():
        if sorted(nums) != list(range(1, len(nums) + 1)):
            raise ValueError("leaf numbers must be continuous from 01")
    return leaves


def load_pack(name: str = "generic") -> TemplatePack:
    return validate_pack(name)


def load_released_pack(name: str = "generic") -> TemplatePack:
    """Load a runtime pack; draft packs remain explicit-load only."""
    if name in _UNRELEASED_PACKS:
        raise ValueError(f"template pack {name!r} is not released")
    return load_pack(name)


def validate_pack(name: str = "generic") -> TemplatePack:
    code = "<pack>"
    try:
        identifier(name)
        path = TEMPLATES_ROOT / name / "leaves.yaml"
        if not path.resolve().is_relative_to(TEMPLATES_ROOT.resolve()):
            raise ValueError("pack path escapes registry")
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
        if not isinstance(data, dict) or data.get("pack") != name:
            raise ValueError("pack name mismatch")
        if type(data.get("version")) is not int or data["version"] < 1:
            raise ValueError("version must be a positive integer")
        if type(data.get("is_industry_pack")) is not bool:
            raise ValueError("is_industry_pack must be boolean")
        trunks, raw_leaves = data.get("trunks"), data.get("leaves")
        if not isinstance(trunks, list) or not isinstance(raw_leaves, list):
            raise ValueError("trunks/leaves must be lists")
        if name == "generic-v2":
            leaves = _v2_leaves(raw_leaves, _v2_trunk_codes(trunks))
            digest = _manifest_digest(name, data["version"], data["is_industry_pack"],
                                      trunks, raw_leaves)
            return TemplatePack(name, data["version"], data["is_industry_pack"],
                                tuple(MappingProxyType(dict(t)) for t in trunks),
                                tuple(leaves), digest)
        expected = {m.key: m.order for m in MODULES}
        seen_trunks = set()
        for trunk in trunks:
            if not isinstance(trunk, dict):
                raise ValueError("trunk must be a mapping")
            code = trunk.get("code")
            if not isinstance(code, str) or code not in expected or code in seen_trunks:
                raise ValueError("unknown or duplicate trunk")
            if type(trunk.get("order")) is not int or trunk["order"] != expected[code]:
                raise ValueError("trunk order mismatch")
            _segment(trunk.get("directory"))
            if "built_in_hidden_default" in trunk and type(trunk["built_in_hidden_default"]) is not bool:
                raise ValueError("hidden default must be boolean")
            if set(trunk) - {"code", "order", "directory", "built_in_hidden_default"}:
                raise ValueError("unknown trunk fields")
            seen_trunks.add(code)
        if seen_trunks != MODULE_KEYS:
            raise ValueError("must declare the ten existing trunks")
        leaves, seen, numbers = [], set(), {key: [] for key in MODULE_KEYS}
        for leaf in raw_leaves:
            if not isinstance(leaf, dict):
                raise ValueError("leaf must be a mapping")
            code = leaf.get("code")
            if set(leaf) != {"code", "trunk", "title", "shape", "scope"}:
                raise ValueError("invalid leaf fields")
            if any(not isinstance(leaf[k], str) or (k != "scope" and not leaf[k].strip()) for k in leaf):
                raise ValueError("leaf fields must be strings (only scope may be empty)")
            parts = code.split("-")
            if (len(parts) != 3 or parts[0] != "L" or parts[1] not in MODULE_KEYS
                    or len(parts[2]) != 2 or any(c not in "0123456789" for c in parts[2])):
                raise ValueError("invalid leaf code")
            if leaf["trunk"] != parts[1]:
                raise ValueError("leaf trunk differs from code")
            if leaf["shape"] not in {"n", "s"}:
                raise ValueError("shape must be n/s")
            if code in seen:
                raise ValueError("duplicate leaf code")
            seen.add(code)
            numbers[parts[1]].append(int(parts[2]))
            leaves.append(LeafCode(**leaf))
        for trunk, nums in numbers.items():
            if sorted(nums) != list(range(1, len(nums) + 1)):
                code = f"L-{trunk}-<NN>"
                raise ValueError("leaf numbers must be continuous from 01")
        if name == "generic" and (len(leaves) != 98 or Counter(l.trunk for l in leaves) != _GENERIC_COUNTS
                                  or data["is_industry_pack"]):
            raise ValueError("generic must contain the prescribed 98 leaves")
        digest = _manifest_digest(name, data["version"], data["is_industry_pack"],
                                  trunks, raw_leaves)
        return TemplatePack(name, data["version"], data["is_industry_pack"],
                            tuple(MappingProxyType(dict(t)) for t in trunks), tuple(leaves),
                            digest)
    except (OSError, yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f"pack {name!r}, code {code!r}: {exc}") from exc


def available_packs() -> list[str]:
    """Released packs visible for company binding; drafts stay explicit-load only."""
    result = []
    for path in sorted(TEMPLATES_ROOT.iterdir()):
        if path.is_dir() and (path / "leaves.yaml").is_file():
            if path.name in _UNRELEASED_PACKS:
                continue
            try:
                load_pack(path.name)
            except ValueError:
                continue
            result.append(path.name)
    return result
