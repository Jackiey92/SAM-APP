"""E3b closing slice: AST nails for the three offline E3b modules.

The E3b production modules are exercised behaviourally by the e3b test
family, but behaviour tests cannot prove that production wiring never
imports or calls them before the separately approved I slice. These tests
freeze three things:

* acceptance 22 core: no production module under ``app/`` imports the three
  new modules or calls their entry points (the three modules themselves are
  excluded from the scan so current_ledger's legal import of decision is not
  wiring);
* the dangerous surface of each module is an exact-set frozen surface:
  no network/subprocess/dynamic-execution face, the decision and ledger
  modules are strictly read-only, and restriction_append may only land its
  formal receipt inside its single commit scope while reusing D's lock;
* the cumulative AST freeze chain (verifier read-only surface, D d92
  surface, E2 host-service ban, E3a adapter surface) stays unbroken when
  this file runs.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "app" / "facts_tree"

DECISION_PATH = FACTS / "recovery_approval_decision.py"
LEDGER_PATH = FACTS / "recovery_current_ledger.py"
APPEND_PATH = FACTS / "recovery_restriction_append.py"
NEW_PATHS = {DECISION_PATH.resolve(), LEDGER_PATH.resolve(), APPEND_PATH.resolve()}

NEW_MODULES = (
    "app.facts_tree.recovery_approval_decision",
    "app.facts_tree.recovery_current_ledger",
    "app.facts_tree.recovery_restriction_append",
)
NEW_MODULE_SHORT = {name.rsplit(".", 1)[1] for name in NEW_MODULES}
ENTRIES = {
    "decide_recovery_admission",
    "verify_current_ledger",
    "append_restrictions",
    "AppendLayout",
}

FORBIDDEN_NAMES = {
    "subprocess", "socket", "Popen", "popen", "system", "pickle",
    "__import__", "importlib", "eval", "exec", "compile",
    "create_case", "open_gate", "requests",
}
FORBIDDEN_ATTRS = {
    "socket", "Popen", "O_CREAT", "O_EXCL", "O_TRUNC", "O_APPEND", "O_RDWR",
}


def _parse(path):
    return ast.parse(path.read_text())


def _imported_modules(tree):
    """All imported module paths, top-level and function-local."""
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def _top_level_modules(tree):
    modules = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    return modules


def _resolve_relative(path, node):
    package = list(path.relative_to(ROOT).parts[:-1])
    base = package[:len(package) - (node.level - 1)]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _hits_module(reference):
    return reference in NEW_MODULES or any(
        reference.startswith(name + ".") for name in NEW_MODULES)


# ----------------------------------------------------- (a) zero production wiring

def test_no_production_module_wires_the_e3b_entries():
    hits = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.resolve() in NEW_PATHS:
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _hits_module(alias.name):
                        hits.append((str(path), node.lineno, "import", alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    reference = node.module or ""
                    bound = {alias.name for alias in node.names}
                else:
                    reference = _resolve_relative(path, node)
                    bound = {alias.name for alias in node.names}
                if _hits_module(reference):
                    hits.append((str(path), node.lineno, "import-from", reference))
                elif reference == "app.facts_tree" and bound & NEW_MODULE_SHORT:
                    hits.append((str(path), node.lineno,
                                 "import-from-package", sorted(bound & NEW_MODULE_SHORT)))
            elif isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
                if name in ENTRIES:
                    hits.append((str(path), node.lineno, "call", name))
    assert hits == [], hits


# ------------------------------------------ (b) frozen dangerous surfaces per module

def _assert_common_danger_surface(path, tree):
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names & FORBIDDEN_NAMES), (path.name, names & FORBIDDEN_NAMES)
    assert not (attrs & FORBIDDEN_ATTRS), (path.name, attrs & FORBIDDEN_ATTRS)
    modules = _imported_modules(tree)
    assert not any(m == "subprocess" or m.startswith("subprocess.")
                   or m == "socket" or m.startswith("socket.")
                   or m.startswith("app.host_") for m in modules), (path.name, modules)
    return names, attrs, modules


def test_decision_and_current_ledger_are_frozen_read_only_modules():
    expected_imports = {
        DECISION_PATH: {
            "__future__", "dataclasses", "hashlib", "json",
            "app.facts_tree.recovery_approval_audit",
            "app.facts_tree.recovery_epoch",
            "app.facts_tree.recovery_state_store",
        },
        LEDGER_PATH: {
            "__future__", "datetime", "hashlib", "os", "stat",
            "app.facts_tree.recovery_approval_decision",
            "app.facts_tree.recovery_epoch",
            "app.facts_tree.recovery_verifier",
        },
    }
    # No storage/SQL write face and no filesystem mutation face may appear in
    # either module: the decision is pure, the ledger opens immutable SQLite
    # via recovery_verifier helpers and otherwise only pins read-only fds.
    storage_write_attrs = {
        "execute", "executemany", "executescript", "commit", "rollback",
        "create_case", "open_gate", "authenticate", "transition", "merge",
    }
    fs_mutating_os_attrs = {
        "mkdir", "makedirs", "unlink", "remove", "rename", "replace",
        "write", "rmdir", "symlink", "link",
    }
    for path in (DECISION_PATH, LEDGER_PATH):
        tree = _parse(path)
        assert _top_level_modules(tree) == expected_imports[path], (
            path.name, _top_level_modules(tree) ^ expected_imports[path])
        assert _imported_modules(tree) <= expected_imports[path], path.name
        names, _attrs, _modules = _assert_common_danger_surface(path, tree)
        assert "RecoveryStateStore" not in names, path.name
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RecoveryStateStore"
            for node in ast.walk(tree)), path.name
        called_attrs = {node.func.attr for node in ast.walk(tree)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)}
        assert not (called_attrs & storage_write_attrs), (
            path.name, called_attrs & storage_write_attrs)
        os_calls = {node.func.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"}
        assert not (os_calls & fs_mutating_os_attrs), (
            path.name, os_calls & fs_mutating_os_attrs)
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"open", "exec", "eval", "__import__"}
            for node in ast.walk(tree)), path.name


def _enclosing(tree):
    scope = {}

    def walk(node, owner):
        if isinstance(node, ast.FunctionDef):
            owner = node.name
        scope[id(node)] = owner
        for child in ast.iter_child_nodes(node):
            walk(child, owner)
    walk(tree, "")
    return scope


def test_restriction_append_frozen_surface_and_single_commit_receipt_scope():
    tree = _parse(APPEND_PATH)
    expected = {
        "__future__", "dataclasses", "json", "os", "stat",
        "app.facts_tree.recovery_epoch",
        "app.facts_tree.recovery_materializer",
        "app.facts_tree.recovery_state_store",
        "app.facts_tree.recovery_reauthorize",
    }
    assert _top_level_modules(tree) == expected, (
        _top_level_modules(tree) ^ expected)
    assert _imported_modules(tree) <= expected

    names, attrs, _modules = _assert_common_danger_surface(APPEND_PATH, tree)
    # No own lock file may ever be created: D's read-only flock helper is the
    # only lock face; fcntl and file-creation flags must stay absent.
    assert "fcntl" not in names
    assert "flock" not in attrs
    assert "RecoveryStateStore" not in names
    assert not ({n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
                & {"create_case", "open_gate", "authenticate", "transition"})

    scope = _enclosing(tree)

    def scopes(call_predicate):
        return {scope[id(node)] for node in ast.walk(tree)
                if isinstance(node, ast.Call) and call_predicate(node)}

    # D's existing lock is acquired exactly once, in the boundary body, never
    # created here.
    acquire = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name)
               and node.func.id == "_acquire_lock"]
    assert len(acquire) == 1
    assert {scope[id(node)] for node in acquire} == {"_append_restrictions"}

    # Landing a formal receipt (locked tmp create + atomic rename + payload
    # write) exists only inside the single _commit_receipt scope.
    create_locked = scopes(lambda n: isinstance(n.func, ast.Name)
                           and n.func.id == "_create_locked_at")
    renames = scopes(lambda n: isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "os" and n.func.attr == "rename")
    payload_writes = scopes(lambda n: isinstance(n.func, ast.Attribute)
                            and isinstance(n.func.value, ast.Name)
                            and n.func.value.id == "os" and n.func.attr == "write")
    assert create_locked == {"_commit_receipt"}
    assert renames == {"_commit_receipt"}
    assert payload_writes == {"_commit_receipt"}

    # unlink is reserved for void kill-residue tmp names; a landed receipt is
    # never unlinked.
    unlinks = scopes(lambda n: isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == "os" and n.func.attr == "unlink")
    assert unlinks == {"_reap_void_tmp", "_discard_tmp"}

    # The store face is exactly read/merge/anchor/close: no creation, auth,
    # transition or gate through this boundary.
    store_calls = {node.func.attr for node in ast.walk(tree)
                   if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Name)
                   and node.func.value.id == "store"}
    assert store_calls == {"get", "merge", "journal_anchor", "close"}


# ---------------------------------------------------- (c) cumulative freeze chain

def test_e3b_keeps_the_cumulative_ast_freeze_chain_alive():
    from tests.test_approval_use_e2 import test_no_production_instantiations
    from tests.test_facts_tree_recovery_reauthorize import (
        test_d92_production_and_test_ast_surface,
    )
    from tests.test_recovery_approval_e3a import (
        test_new_adapter_ast_surface_and_existing_verifier_imports_are_frozen,
    )
    test_no_production_instantiations()
    test_d92_production_and_test_ast_surface()
    test_new_adapter_ast_surface_and_existing_verifier_imports_are_frozen()
