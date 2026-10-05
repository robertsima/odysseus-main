#!/usr/bin/env python3
"""Check that a PR's new tests fail without the PR's production change.

For a pull request that changes production code, this finds the test functions
the PR adds or changes, runs them against the base commit's production code
(with the PR's whole tests/ folder laid over it), and again on the head commit.
Each one should fail or error on base and pass on head. A test that passes on
base proves nothing about the change. See website/testing-restructure-2026-10-03.md
(D6, D17).

Moved tests are not new: a function whose body (ignoring its name) already
exists anywhere in the base commit's tests is skipped. Node tests (*.test.mjs)
are checked per file.

Usage:
  red_evidence.py --base <sha> --head <sha> [--labels "a,b"] [--enforce]

Writes a Markdown report to stdout. Exits 1 only with --enforce and a failure.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PRODUCTION_PREFIXES = ("core/", "src/", "routes/", "services/", "static/", "app.py")
SKIP_LABELS = {"refactor"}


def git(*args: str, cwd: str | Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def changed_files(base: str, head: str) -> list[tuple[str, str]]:
    """(status, path) for files changed between base and head, renames as adds."""
    out = git("diff", "--name-status", "--no-renames", f"{base}...{head}")
    rows = []
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        rows.append((status[:1], path))
    return rows


def touches_production(paths: list[str]) -> bool:
    return any(p == "app.py" or p.startswith(PRODUCTION_PREFIXES) for p in paths)


def is_python_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") and path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_node_test(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".test.mjs")


def _body_hash(node: ast.AST) -> str:
    clone = ast.parse(ast.unparse(node)).body[0]
    clone.name = "_"
    return hashlib.sha1(ast.dump(clone, annotate_fields=False).encode()).hexdigest()


def test_functions(source: str) -> dict[str, str]:
    """Map test node-id suffixes ("test_x" or "TestC::test_x") to body hashes."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            found[node.name] = _body_hash(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    found[f"{node.name}::{item.name}"] = _body_hash(item)
    return found


def file_at(rev: str, path: str) -> str | None:
    try:
        return git("show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None


def base_hashes(base: str) -> set[str]:
    hashes: set[str] = set()
    for path in git("ls-tree", "-r", "--name-only", base, "tests/").splitlines():
        if is_python_test(path):
            hashes.update(test_functions(file_at(base, path) or "").values())
    return hashes


@dataclass
class Candidates:
    python: list[str] = field(default_factory=list)   # pytest node ids
    node: list[str] = field(default_factory=list)     # .test.mjs paths
    moved: int = 0


def new_or_changed_tests(base: str, head: str, rows: list[tuple[str, str]]) -> Candidates:
    known = base_hashes(base)
    picked = Candidates()
    for status, path in rows:
        if status == "D":
            continue
        if is_node_test(path):
            if file_at(base, path) != file_at(head, path):
                picked.node.append(path)
            continue
        if not is_python_test(path):
            continue
        before = test_functions(file_at(base, path) or "")
        after = test_functions(file_at(head, path) or "")
        for name, digest in after.items():
            if before.get(name) == digest:
                continue
            if digest in known:
                picked.moved += 1
                continue
            picked.python.append(f"{path}::{name}")
    return picked


_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


def parse_summary(output: str, node_ids: list[str]) -> dict[str, str]:
    """Outcome per node id from pytest's ``-rA`` summary lines. Parametrized
    cases fold into their function (worst case wins). A collection error on a
    file marks all of that file's tests ``error``; a test never reported is
    ``error`` too, since the base commit could not even collect it."""
    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        word, _, rest = line.partition(" ")
        state = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error", "SKIPPED": "skipped"}.get(word)
        if not state:
            continue
        reported = rest.split(" - ", 1)[0].strip()
        for nid in node_ids:
            if reported == nid or reported.startswith(nid + "[") or reported == nid.split("::", 1)[0]:
                if _RANK[state] >= _RANK.get(outcomes.get(nid, "passed"), 0) or nid not in outcomes:
                    outcomes[nid] = state
    for nid in node_ids:
        outcomes.setdefault(nid, "error")
    return outcomes


def run_pytest(cwd: Path, node_ids: list[str]) -> dict[str, str]:
    """Outcome per node id: passed, failed, error or skipped."""
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "--tb=no", "-p", "no:cacheprovider",
                           *node_ids], cwd=cwd, capture_output=True, text=True)
    return parse_summary(proc.stdout + proc.stderr, node_ids)


def run_node(cwd: Path, files: list[str]) -> dict[str, str]:
    outcomes = {}
    for path in files:
        proc = subprocess.run(["node", "--test", path], cwd=cwd, capture_output=True, text=True)
        outcomes[path] = "passed" if proc.returncode == 0 else "failed"
    return outcomes


def base_with_head_tests(base: str, head_root: Path, scratch: Path) -> Path:
    tree = scratch / "base"
    git("worktree", "add", "--detach", str(tree), base, cwd=head_root)
    shutil.rmtree(tree / "tests")
    shutil.copytree(head_root / "tests", tree / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    if (head_root / "node_modules").is_dir() and not (tree / "node_modules").exists():
        os.symlink(head_root / "node_modules", tree / "node_modules", target_is_directory=True)
    (tree / "data").mkdir(exist_ok=True)
    return tree


def verdict(base_state: str, head_state: str) -> str:
    if head_state != "passed":
        return "fails on head"
    if base_state in ("failed", "error"):
        return "ok: red on base, green on head"
    return "passes without the change"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--labels", default="")
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args()

    print("## Red evidence\n")
    labels = {label.strip() for label in args.labels.split(",") if label.strip()}
    if labels & SKIP_LABELS:
        print("Skipped: the PR is labelled `refactor`.")
        return 0
    rows = changed_files(args.base, args.head)
    if not touches_production([p for _, p in rows]):
        print("Skipped: no production code changed.")
        return 0
    picked = new_or_changed_tests(args.base, args.head, rows)
    if not picked.python and not picked.node:
        print("This PR changes production code but adds or changes no tests.")
        return 1 if args.enforce else 0

    head_root = Path(git("rev-parse", "--show-toplevel").strip())
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        base_tree = base_with_head_tests(args.base, head_root, scratch)
        try:
            base_py = run_pytest(base_tree, picked.python) if picked.python else {}
            head_py = run_pytest(head_root, picked.python) if picked.python else {}
            base_js = run_node(base_tree, picked.node)
            head_js = run_node(head_root, picked.node)
        finally:
            git("worktree", "remove", "--force", str(base_tree), cwd=head_root)

    results = [(t, base_py.get(t, "error"), head_py.get(t, "error")) for t in picked.python]
    results += [(t, base_js[t], head_js[t]) for t in picked.node]
    print("| Test | Base | Head | Verdict |\n|---|---|---|---|")
    bad = 0
    for test, base_state, head_state in results:
        result = verdict(base_state, head_state)
        bad += not result.startswith("ok")
        print(f"| `{test}` | {base_state} | {head_state} | {result} |")
    if picked.moved:
        print(f"\n{picked.moved} moved test(s) skipped: their bodies already exist in the base commit.")
    if bad:
        print("\nA test that passes on base does not show the change works. Make it fail without the "
              "fix, or label the PR `refactor` and say why in the description.")
    return 1 if bad and args.enforce else 0


if __name__ == "__main__":
    raise SystemExit(main())
