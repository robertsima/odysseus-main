"""Find test-suite patterns that leak state between tests or pin source text.

Used by tests/suite/test_hygiene.py, which fails on any offender missing from
tests/suite/hygiene_allowlist.json and on any allowlisted file that no longer
offends, so the allowlist only shrinks.

Rules (website/testing-restructure-2026-10-03.md, D5 and D12):
- sys_modules_module_scope: a test module writes ``sys.modules`` while it is
  imported. The stub outlives the module and changes what later tests import.
- importlib_reload: ``importlib.reload`` swaps module objects under other tests
  that already hold references to the old ones.
- raw_environ_write: ``os.environ`` written without ``monkeypatch``, so the value
  outlives the test.
- source_text_read: a test reads production source as text (``read_text`` or
  ``open`` on a production path, ``inspect.getsource``, ``ast.parse`` of a file).
  Such tests break on harmless reformatting and pass when behavior breaks.
- sys_path_write: a test edits ``sys.path``. tests/conftest.py already puts the
  repo root there, and a wrong entry (``tests/`` itself) makes the mirrored
  ``tests/scripts`` or ``tests/src`` package shadow production ``scripts`` or
  ``src``, which are namespace packages.

Run ``python -m tests.suite.hygiene`` to list offenders, or
``python -m tests.suite.hygiene --prune`` to drop allowlist entries that no
longer offend (after a file was fixed, moved or deleted).
"""
from __future__ import annotations

import ast
import json
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
ALLOWLIST = Path(__file__).with_name("hygiene_allowlist.json")

RULES = ("sys_modules_module_scope", "importlib_reload", "raw_environ_write", "source_text_read", "sys_path_write")
_LIST_WRITES = {"insert", "append", "extend", "remove", "pop", "clear", "__setitem__"}

_PROD_SEGMENTS = {
    "static", "src", "routes", "core", "services", "scripts", "integrations",
    "mcp_servers", "lotus-mcp", "companion", "clients", "templates", "app.py",
}
_TEMP_NAMES = {"tmp_path", "tmpdir", "tmp_path_factory", "tempfile", "TemporaryDirectory"}
_MAPPING_WRITES = {"setdefault", "update", "pop", "popitem", "clear", "__setitem__", "__delitem__"}
_READS = {"read_text", "read_bytes"}


def test_modules() -> list[Path]:
    """Test modules under tests/, excluding the plugins and this guard."""
    skip = {TESTS / "plugins", TESTS / "suite"}
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        if any(parent in skip for parent in path.parents):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            found.append(path)
    return found


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


class _Scanner(ast.NodeVisitor):
    def __init__(self, tree: ast.Module):
        self.hits: set[str] = set()
        self.depth = 0  # >0 inside a function body
        self.assigns: dict[str, list[ast.AST]] = {}
        self.environ_names = {"os.environ"}
        self.reload_names = {"importlib.reload"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assigns.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name) and node.value:
                self.assigns.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        self.environ_names.add(alias.asname or "environ")
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "reload":
                        self.reload_names.add(alias.asname or "reload")

    # --- scopes -----------------------------------------------------------
    def _function(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    # --- writes -------------------------------------------------------------
    def _subscript_of(self, target: ast.AST, names: set[str]) -> bool:
        return isinstance(target, ast.Subscript) and _dotted(target.value) in names

    def _check_targets(self, targets):
        for target in targets:
            for node in ast.walk(target):
                if self._subscript_of(node, {"sys.modules"}) and self.depth == 0:
                    self.hits.add("sys_modules_module_scope")
                if self._subscript_of(node, self.environ_names):
                    self.hits.add("raw_environ_write")
                if _dotted(node) == "sys.path" or self._subscript_of(node, {"sys.path"}):
                    self.hits.add("sys_path_write")

    def visit_Assign(self, node):
        self._check_targets(node.targets)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._check_targets([node.target])
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._check_targets([node.target])
        self.generic_visit(node)

    def visit_Delete(self, node):
        self._check_targets(node.targets)
        self.generic_visit(node)

    # --- calls --------------------------------------------------------------
    def visit_Call(self, node):
        name = _dotted(node.func)
        if isinstance(node.func, ast.Attribute):
            owner = _dotted(node.func.value)
            if owner == "sys.modules" and node.func.attr in _MAPPING_WRITES and self.depth == 0:
                self.hits.add("sys_modules_module_scope")
            if owner in self.environ_names and node.func.attr in _MAPPING_WRITES:
                self.hits.add("raw_environ_write")
            if owner == "sys.path" and node.func.attr in _LIST_WRITES:
                self.hits.add("sys_path_write")
            if node.func.attr in _READS and self._names_production(node.func.value):
                self.hits.add("source_text_read")
        if name in self.reload_names:
            self.hits.add("importlib_reload")
        if name in {"os.putenv", "os.unsetenv"}:
            self.hits.add("raw_environ_write")
        if name in {"inspect.getsource", "inspect.getsourcelines", "getsource", "getsourcelines"}:
            self.hits.add("source_text_read")
        if name in {"open", "io.open", "builtins.open"} and node.args and self._names_production(node.args[0]):
            self.hits.add("source_text_read")
        if name == "ast.parse" and node.args and not isinstance(node.args[0], ast.Constant):
            if self._names_production(node.args[0]):
                self.hits.add("source_text_read")
        self.generic_visit(node)

    # --- does an expression point at production source? ---------------------
    def _leaves(self, node: ast.AST, depth: int = 0, seen: frozenset = frozenset()) -> tuple[list[str], set[str]]:
        """String constants and bare names an expression is built from, following
        local assignments a few levels deep."""
        strings: list[str] = []
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                strings.append(sub.value)
            elif isinstance(sub, ast.Name):
                names.add(sub.id)
                if depth < 4 and sub.id not in seen:
                    for value in self.assigns.get(sub.id, ()):
                        more_strings, more_names = self._leaves(value, depth + 1, seen | {sub.id})
                        strings.extend(more_strings)
                        names |= more_names
        return strings, names

    def _names_production(self, node: ast.AST) -> bool:
        strings, names = self._leaves(node)
        if names & _TEMP_NAMES:
            return False  # a fake tree the test built, not the repo
        for text in strings:
            parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
            if parts and parts[0] in _PROD_SEGMENTS:
                return True
        return False


@lru_cache(maxsize=None)
def scan() -> dict[str, set[str]]:
    """Map each rule to the set of repo-relative test paths that break it."""
    found: dict[str, set[str]] = {rule: set() for rule in RULES}
    for path in test_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        scanner = _Scanner(tree)
        scanner.visit(tree)
        rel = path.relative_to(ROOT).as_posix()
        for rule in scanner.hits:
            found[rule].add(rel)
    return found


def load_allowlist() -> dict[str, dict[str, str]]:
    data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    return {rule: dict(data.get(rule, {})) for rule in RULES}


def write_allowlist(data: dict[str, dict[str, str]]) -> None:
    ordered = {rule: dict(sorted(data.get(rule, {}).items())) for rule in RULES}
    ALLOWLIST.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    found = scan()
    if "--prune" in argv:
        allow = load_allowlist()
        for rule in RULES:
            allow[rule] = {p: r for p, r in allow[rule].items() if p in found[rule]}
        write_allowlist(allow)
        print(f"pruned {ALLOWLIST.relative_to(ROOT)}")
        return 0
    for rule in RULES:
        print(f"{rule}: {len(found[rule])}")
        for path in sorted(found[rule]):
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
