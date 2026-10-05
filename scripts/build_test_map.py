#!/usr/bin/env python3
"""Turn a per-test coverage file into test_map.json for scripts/affected_tests.py.

The nightly workflow runs the suite with ``--cov-context=test``, so every
covered line records the tests that executed it. This writes, for each
production file, the test files whose tests executed any of its lines.

Usage: scripts/build_test_map.py [--coverage .coverage] [--out test_map.json]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_file_of(context: str) -> str | None:
    """pytest-cov contexts look like ``tests/x/test_y.py::test_z|run``."""
    if not context or "::" not in context:
        return None
    return context.split("::", 1)[0].replace("\\", "/")


def build(coverage_path: str) -> dict[str, list[str]]:
    from coverage import CoverageData

    data = CoverageData(basename=coverage_path)
    data.read()
    files: dict[str, list[str]] = {}
    for measured in data.measured_files():
        try:
            rel = Path(measured).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if rel.startswith("tests/"):
            continue
        tests = {test_file_of(ctx) for ctxs in (data.contexts_by_lineno(measured) or {}).values() for ctx in ctxs}
        tests.discard(None)
        if tests:
            files[rel] = sorted(tests)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--coverage", default=".coverage")
    parser.add_argument("--out", default="test_map.json")
    args = parser.parse_args()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    payload = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "commit": commit or os.environ.get("GITHUB_SHA", ""),
        "files": build(args.coverage),
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{args.out}: {len(payload['files'])} production files mapped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
