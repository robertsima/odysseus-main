#!/usr/bin/env python3
"""Plant known bugs and harmless refactors, and check what the suite says.

Each patch in tests/mutants/ is applied on its own, the suite runs, and the
patch is reverted. ``B*`` patches plant a realistic bug: some test must newly
fail. ``N*`` patches are behavior-preserving refactors: no test may newly fail.
tests/mutants/expected.json records the outcome each patch should have; the run
fails when a planted bug stops being caught, a refactor starts failing tests,
or a patch no longer applies (refresh it against the current code).

Every real regression CI catches should become a new ``B*`` patch here. See
website/testing-restructure-2026-10-03.md (D28).

Usage: scripts/mutation_benchmark.py [--only B01,N04] [-- extra pytest args]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = ROOT / "tests" / "mutants"
PYTEST = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=no", "-p", "no:cacheprovider",
          "-n", "auto", "-m", "not browser and not nightly"]
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)")


def failing_tests(output: str) -> set[str]:
    return {m.group(1) for line in output.splitlines() if (m := _FAILED.match(line))}


def run_suite(extra: list[str]) -> set[str]:
    proc = subprocess.run(PYTEST + extra, cwd=ROOT, capture_output=True, text=True)
    failed = failing_tests(proc.stdout + proc.stderr)
    node = shutil.which("node")
    if node:
        files = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("*.test.mjs"))
        for path in files:
            if subprocess.run([node, "--test", "--test-timeout=60000", path], cwd=ROOT,
                              capture_output=True).returncode:
                failed.add(path)
    return failed


def outcome(patch_id: str, new_failures: set[str]) -> str:
    if patch_id.startswith("B"):
        return "caught" if new_failures else "missed"
    return "flagged" if new_failures else "clean"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default="")
    args, extra = parser.parse_known_args()
    extra = [a for a in extra if a != "--"]
    expected = json.loads((MUTANTS / "expected.json").read_text(encoding="utf-8"))
    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    patches = [p for p in sorted(MUTANTS.glob("*.patch")) if not wanted or p.stem in wanted]

    baseline = run_suite(extra)
    print(f"baseline: {len(baseline)} failing test(s) before any patch")
    rows, problems = [], []
    for patch in patches:
        pid = patch.stem
        if git("apply", "--check", str(patch)).returncode:
            rows.append((pid, "stale", "-"))
            problems.append(f"{pid}: no longer applies; refresh it against the current code")
            continue
        git("apply", str(patch))
        try:
            new = run_suite(extra) - baseline
        finally:
            git("apply", "-R", str(patch))
        got = outcome(pid, new)
        want = expected.get(pid, {}).get("expect")
        rows.append((pid, got, ", ".join(sorted(new)[:5]) + (" ..." if len(new) > 5 else "")))
        if want and got != want:
            problems.append(f"{pid}: expected {want}, got {got} ({expected[pid].get('what', '')})")

    print("\n| Patch | Result | First new failures |\n|---|---|---|")
    for pid, got, sample in rows:
        print(f"| {pid} | {got} | {sample or '-'} |")
    if problems:
        print("\nRegressions:\n" + "\n".join(f"- {p}" for p in problems))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
