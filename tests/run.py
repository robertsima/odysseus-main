"""Run a test lane: ``python -m tests.run <lane> [--base REF] [-- pytest args]``.

Lanes (website/testing-restructure-2026-10-03.md, D22):

  affected  The tests covering what you changed, from scripts/affected_tests.py.
            Use it while working. Falls back to ``full`` when the change can
            affect any test (conftest, tests/plugins, dependencies).
  full      Everything except browser and nightly tests, in parallel, plus the
            node tests. Use it once before a push or a hand-back.
  browser   Real-browser tests. Use it when static/ or a page-serving route
            changed.
  security  Owner scope, auth, confinement, SSRF, sensitivity and approval tests.
            ``full`` always includes them.
  nightly   The slow tier the nightly workflow runs.

Linux CI decides. On Windows, POSIX-only tests skip, so a green Windows run is
partial.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANES = ("affected", "full", "browser", "security", "nightly")
_PARALLEL_FROM = 15  # below this many files, xdist start-up costs more than it saves


def _has_xdist() -> bool:
    return importlib.util.find_spec("xdist") is not None


def _affected_module():
    spec = importlib.util.spec_from_file_location("affected_tests", ROOT / "scripts" / "affected_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pytest(args: list[str], parallel: bool = False) -> int:
    command = [sys.executable, "-m", "pytest", "-q"]
    if parallel and _has_xdist():
        command += ["-n", "auto"]
    print("$", " ".join(command[1:] + args), flush=True)
    code = subprocess.run(command + args, cwd=ROOT).returncode
    if code == 5:  # pytest: no tests collected, e.g. a marker nothing carries yet
        print("No tests selected.")
        return 0
    return code


def node(files: list[str]) -> int:
    if not files:
        return 0
    exe = shutil.which("node")
    if not exe:
        print("node not found: skipping", len(files), "node test file(s)")
        return 0
    print("$ node --test", " ".join(files), flush=True)
    return subprocess.run([exe, "--test", *files], cwd=ROOT).returncode


def node_tests() -> list[str]:
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("*.test.mjs"))


def run_full(extra: list[str]) -> int:
    if sys.platform == "win32":
        print("Windows run: POSIX-only tests skip here. Linux CI decides.")
    code = pytest(["-m", "not browser and not nightly", *extra], parallel=True)
    return code or node(node_tests())


def run_affected(base: str, extra: list[str]) -> int:
    module = _affected_module()
    tests = module.affected(module.changed_files(base), module.load_map())
    if tests is None:
        print("The change can affect any test: running the full lane.")
        return run_full(extra)
    if not tests:
        print("No tests cover these changes.")
        return 0
    py = [t for t in tests if t.endswith(".py")]
    js = [t for t in tests if t.endswith(".test.mjs")]
    code = pytest([*py, "-m", "not nightly", *extra], parallel=len(py) >= _PARALLEL_FROM) if py else 0
    return code or node(js)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, extra = argv[:at], argv[at + 1:]
    parser = argparse.ArgumentParser(prog="python -m tests.run", description=__doc__.split("\n\n")[0])
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--base", default="origin/dev", help="affected: compare against this ref")
    args = parser.parse_args(argv)

    if args.lane == "affected":
        return run_affected(args.base, extra)
    if args.lane == "full":
        return run_full(extra)
    if args.lane == "browser":
        return pytest(["-m", "browser", *extra])
    if args.lane == "security":
        return pytest(["-m", "security or area_security", *extra], parallel=True)
    return pytest(["-m", "nightly", *extra])


if __name__ == "__main__":
    sys.exit(main())
