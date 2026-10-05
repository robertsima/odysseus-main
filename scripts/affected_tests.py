#!/usr/bin/env python3
"""List the tests that cover a set of changed files.

Sources, in order:
1. The coverage map the nightly workflow publishes on the ``test-map`` branch
   (``test_map.json``: production file -> test files whose tests executed it).
2. The mirrored test folder: tests for ``src/agent_loop.py`` live under
   ``tests/src/agent_loop/``.
3. For files the map has not seen (new files, JS, templates): test files that
   mention the module's import path or the file's name.

A change to the test infrastructure (conftest, tests/plugins, tests/helpers,
pytest config, requirements) can affect any test, so it selects everything.

Usage:
  scripts/affected_tests.py [--base REF] [--map PATH] [FILE ...]

With no FILE arguments, the changed files are those between the merge base
with REF (default origin/dev) and the working tree, untracked files included.
Prints one test path per line, or ``ALL`` when everything is affected.
See website/testing-restructure-2026-10-03.md (D15, D22).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAP_BRANCH = "test-map"
MAP_FILE = "test_map.json"

_EVERYTHING = (
    "tests/conftest.py", "tests/plugins/", "tests/helpers/", "pyproject.toml",
    "requirements.txt", "requirements-test.txt", "package.json", "package-lock.json",
)
_IGNORED_PREFIXES = ("website/", "docs/", ".github/ISSUE_TEMPLATE/", "assets/")


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def changed_files(base: str = "origin/dev") -> list[str]:
    merge_base = _git("merge-base", base, "HEAD").strip() or "HEAD"
    names = set(_git("diff", "--name-only", merge_base).split())
    names |= set(_git("ls-files", "--others", "--exclude-standard").split())
    return sorted(names)


def load_map(path: str | None = None) -> dict[str, list[str]]:
    """The nightly coverage map, from a file or the test-map branch; {} if absent."""
    if path:
        text = Path(path).read_text(encoding="utf-8")
    else:
        _git("fetch", "--quiet", "origin", MAP_BRANCH)
        text = _git("show", f"FETCH_HEAD:{MAP_FILE}") or _git("show", f"origin/{MAP_BRANCH}:{MAP_FILE}")
    if not text:
        return {}
    try:
        return json.loads(text).get("files", {})
    except ValueError:
        return {}


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.startswith("tests/") and (
        (name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")))
        or name.endswith(".test.mjs")
    )


def mirror_folder(path: str) -> str:
    """tests/<path without suffix>/: where D11 puts the tests for ``path``.
    A leading dot is dropped, so .github/ mirrors to tests/github/."""
    pure = PurePosixPath(path)
    parts = [p.lstrip(".") for p in pure.parent.parts]
    return PurePosixPath("tests", *parts, pure.stem).as_posix()


@lru_cache(maxsize=1)
def _test_sources() -> dict[str, str]:
    sources = {}
    for test in sorted((ROOT / "tests").rglob("*")):
        rel = test.relative_to(ROOT).as_posix()
        if test.is_file() and is_test_file(rel):
            sources[rel] = test.read_text(encoding="utf-8", errors="replace")
    return sources


def _mention_patterns(path: str) -> list[re.Pattern]:
    pure = PurePosixPath(path)
    if pure.suffix == ".py":
        dotted = ".".join(pure.with_suffix("").parts)
        names = [dotted] if pure.stem == "__init__" else [dotted, pure.stem]
    else:
        names = [pure.name, pure.stem] if len(pure.stem) > 3 else [pure.name]
    return [re.compile(rf"(?<![\w.-]){re.escape(n)}(?![\w-])") for n in names]


def mentioning(path: str) -> set[str]:
    patterns = _mention_patterns(path)
    return {test for test, text in _test_sources().items() if any(p.search(text) for p in patterns)}


def affected(files: list[str], coverage_map: dict[str, list[str]]) -> list[str] | None:
    """Test files covering ``files``; None means run everything."""
    selected: set[str] = set()
    for path in files:
        if any(path == e or (e.endswith("/") and path.startswith(e)) for e in _EVERYTHING):
            return None
        if path.startswith(_IGNORED_PREFIXES):
            continue
        if path.startswith("tests/"):
            if is_test_file(path) and (ROOT / path).exists():
                selected.add(path)
            continue
        selected.update(t for t in coverage_map.get(path, ()) if (ROOT / t).exists())
        folder = ROOT / mirror_folder(path)
        if folder.is_dir():
            selected.update(t.relative_to(ROOT).as_posix() for t in folder.rglob("*")
                            if t.is_file() and is_test_file(t.relative_to(ROOT).as_posix()))
        if path not in coverage_map:
            selected |= mentioning(path)
    return sorted(selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--map", dest="map_path")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    files = args.files or changed_files(args.base)
    tests = affected(files, load_map(args.map_path))
    print("ALL" if tests is None else "\n".join(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
