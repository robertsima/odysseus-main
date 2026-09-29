"""The language toolchain a workspace's own manifests ask for.

On 2026-09-29 agents could not run Umni's tests: its backend needs Java 21
(``pom.xml``) and the image had no JDK at all, and its mobile app needs Node
24 (``"engines": {"node": ">=24.3.0 <25"}``) while the image's only Node is
the 22 the app itself is pinned to. Workers said "Node 24 was not available"
and never ran the backend suite.

The image now carries extra toolchains under ``/opt/toolchains``
(``node/<major>/bin``, ``java/<major>`` as a JAVA_HOME, ``maven/bin``); the
bash sandbox mounts that folder read-only. This module reads what a
workspace asks for -- ``package.json`` engines, ``.nvmrc``,
``.node-version``, ``.tool-versions``, ``pom.xml``, ``build.gradle``,
``.java-version``, ``.sdkmanrc`` -- picks the best installed match, and gives
the shell a PATH/JAVA_HOME that puts it first. The app's own Node stays the
default when nothing asks for another.

``setup_plan`` also says what a fresh checkout needs before its tests can run
(a worktree has no ``node_modules``: "jest: not found").
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ROOT_ENV = "ODYSSEUS_TOOLCHAINS_DIR"
DEFAULT_ROOT = "/opt/toolchains"

Version = Tuple[int, int, int]

# Folders a manifest scan never enters: dependencies, build output, VCS.
_SKIP_DIRS = frozenset({
    "node_modules", ".git", "target", "build", "dist", "out", "vendor", ".venv", "venv",
    "__pycache__", ".gradle", ".idea", ".vscode", "coverage", ".next", ".expo", ".turbo",
    ".cache", "bin", "obj",
})
_SCAN_DEPTH = 2
_SCAN_MAX_DIRS = 400
_CACHE_TTL_S = 30.0


def root() -> str:
    return os.environ.get(ROOT_ENV) or DEFAULT_ROOT


# ── versions and ranges ────────────────────────────────────────────────────

def parse_version(text: str) -> Optional[Version]:
    m = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def format_version(v: Version) -> str:
    return ".".join(str(p) for p in v)


def _partial(text: str) -> Tuple[List[Optional[int]], bool]:
    """``"24.3"`` -> ([24, 3, None], True); wildcards ("x", "*") become None."""
    text = text.strip().lstrip("v=")
    text = re.sub(r"[-+].*$", "", text)       # pre-release / build metadata
    parts: List[Optional[int]] = []
    for piece in (text.split(".") if text else []):
        if piece in ("x", "X", "*", ""):
            parts.append(None)
        elif piece.isdigit():
            parts.append(int(piece))
        else:
            return [], False
    while len(parts) < 3:
        parts.append(None)
    return parts[:3], True


def _bounds(op: str, spec: str) -> Optional[Tuple[Optional[Version], bool, Optional[Version], bool]]:
    """(low, low_inclusive, high, high_inclusive) for one comparator."""
    parts, ok = _partial(spec)
    if not ok:
        return None
    major, minor, patch = parts
    if major is None:
        return None, True, None, True          # "*": anything
    lo = (major, minor or 0, patch or 0)
    if op in ("", "="):
        if minor is None:
            return lo, True, (major + 1, 0, 0), False
        if patch is None:
            return lo, True, (major, minor + 1, 0), False
        return lo, True, lo, True
    if op == "^":
        if major > 0 or minor is None:
            return lo, True, (major + 1, 0, 0), False
        if minor > 0 or patch is None:
            return lo, True, (0, minor + 1, 0), False
        return lo, True, (0, 0, patch + 1), False
    if op == "~":
        if minor is None:
            return lo, True, (major + 1, 0, 0), False
        return lo, True, (major, minor + 1, 0), False
    if op == ">=":
        return lo, True, None, True
    if op == ">":
        if minor is None:
            return (major + 1, 0, 0), True, None, True
        if patch is None:
            return (major, minor + 1, 0), True, None, True
        return lo, False, None, True
    if op == "<":
        return None, True, lo, False
    if op == "<=":
        if minor is None:
            return None, True, (major + 1, 0, 0), False
        if patch is None:
            return None, True, (major, minor + 1, 0), False
        return None, True, lo, True
    return None


def _in_bounds(v: Version, b) -> bool:
    lo, lo_inc, hi, hi_inc = b
    if lo is not None and (v < lo or (v == lo and not lo_inc)):
        return False
    if hi is not None and (v > hi or (v == hi and not hi_inc)):
        return False
    return True


def satisfies(version: Version, spec: str) -> Optional[bool]:
    """Whether ``version`` is in the npm-style range ``spec``; None if unreadable.

    Covers what engines fields and version files use: ``>=24.3.0 <25``,
    ``^20``, ``~18.2``, ``22.x``, ``24``, ``a - b``, ``||``.
    """
    spec = str(spec or "").strip()
    if not spec or spec in ("*", "x", "latest", "node", "current"):
        return True
    readable = False
    for alternative in spec.split("||"):
        alternative = alternative.strip()
        if not alternative:
            continue
        hyphen = re.match(r"^(\S+)\s+-\s+(\S+)$", alternative)
        if hyphen:
            low, high = _bounds(">=", hyphen.group(1)), _bounds("<=", hyphen.group(2))
            if low is None or high is None:
                continue
            readable = True
            if _in_bounds(version, low) and _in_bounds(version, high):
                return True
            continue
        # ">= 24" -> ">=24"
        tokens = re.findall(r"(>=|<=|>|<|=|\^|~)?\s*(v?[\dxX*][\w.*+-]*)", alternative)
        if not tokens:
            continue
        bounds = [_bounds(op or "", ver) for op, ver in tokens]
        if any(b is None for b in bounds):
            continue
        readable = True
        if all(_in_bounds(version, b) for b in bounds):
            return True
    return False if readable else None


# ── what is installed ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Toolchain:
    kind: str                 # "node" | "java" | "maven"
    version: Version
    home: str                 # the folder; JAVA_HOME for java
    system: bool = False      # the default on PATH, not one under the root

    @property
    def bin(self) -> str:
        return os.path.join(self.home, "bin")

    def label(self) -> str:
        return f"{self.kind} {format_version(self.version)}"


_installed_lock = threading.Lock()
_installed_cache: Dict[str, Tuple[float, Dict[str, List[Toolchain]]]] = {}


def _run_version(argv: Sequence[str]) -> str:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=5)
        return (done.stdout or done.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _java_version(home: str) -> Optional[Version]:
    release = os.path.join(home, "release")
    try:
        with open(release, encoding="utf-8", errors="replace") as fh:
            m = re.search(r'^JAVA_VERSION="([^"]+)"', fh.read(), re.M)
        if m:
            text = m.group(1)
            return (8, 0, 0) if text.startswith("1.8") else parse_version(text)
    except OSError:
        pass
    return parse_version(os.path.basename(home))


def installed() -> Dict[str, List[Toolchain]]:
    """Toolchains under the root, plus the Node already on PATH (the default)."""
    base = root()
    now = time.monotonic()
    with _installed_lock:
        cached = _installed_cache.get(base)
        if cached and now - cached[0] < 300:
            return cached[1]
    found: Dict[str, List[Toolchain]] = {"node": [], "java": [], "maven": []}
    for home in _subdirs(os.path.join(base, "node")):
        node = os.path.join(home, "bin", "node")
        if os.access(node, os.X_OK):
            version = parse_version(_run_version([node, "--version"])) or parse_version(os.path.basename(home))
            if version:
                found["node"].append(Toolchain("node", version, home))
    for home in _subdirs(os.path.join(base, "java")):
        if os.access(os.path.join(home, "bin", "java"), os.X_OK):
            version = _java_version(home)
            if version:
                found["java"].append(Toolchain("java", version, home))
    maven = os.path.join(base, "maven")
    if os.access(os.path.join(maven, "bin", "mvn"), os.X_OK):
        found["maven"].append(Toolchain("maven", parse_version(_run_version_file(maven)) or (3, 0, 0), maven))
    system_node = shutil.which("node")
    if system_node and not os.path.realpath(system_node).startswith(os.path.realpath(base) + os.sep):
        version = parse_version(_run_version([system_node, "--version"]))
        if version and all(t.version != version for t in found["node"]):
            found["node"].append(Toolchain("node", version, os.path.dirname(os.path.dirname(system_node)),
                                           system=True))
    for kind in found:
        found[kind].sort(key=lambda t: t.version)
    with _installed_lock:
        _installed_cache[base] = (now, found)
    return found


def _subdirs(path: str) -> List[str]:
    try:
        return [os.path.join(path, n) for n in sorted(os.listdir(path)) if os.path.isdir(os.path.join(path, n))]
    except OSError:
        return []


def _run_version_file(maven_home: str) -> str:
    """Maven's version from its lib/maven-core jar name (no JVM start)."""
    try:
        for name in os.listdir(os.path.join(maven_home, "lib")):
            m = re.match(r"maven-core-(\d+\.\d+\.\d+)\.jar$", name)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


# ── what a workspace asks for ──────────────────────────────────────────────

@dataclass
class Requirements:
    node: List[Tuple[str, str]] = field(default_factory=list)     # (source, range)
    java: List[Tuple[str, int]] = field(default_factory=list)     # (source, major)
    npm_dirs: List[Tuple[str, str]] = field(default_factory=list)  # (dir, lockfile)
    maven_dirs: List[str] = field(default_factory=list)
    gradle_dirs: List[str] = field(default_factory=list)
    testcontainers: List[str] = field(default_factory=list)       # build files that use it


def _read(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _java_major(text: str) -> Optional[int]:
    text = str(text or "").strip().strip("'\"")
    m = re.match(r"^(?:[a-z]+-)?1\.(\d+)", text)           # 1.8 -> 8
    if m:
        return int(m.group(1))
    m = re.match(r"^(?:[a-z]+-)?(\d+)", text)              # 21, 21.0.2, temurin-21.0.2
    return int(m.group(1)) if m else None


_POM_JAVA_RE = re.compile(
    r"<(java\.version|maven\.compiler\.release|maven\.compiler\.target|maven\.compiler\.source|release)>"
    r"\s*([^<\s]+)\s*</\1>")
_GRADLE_JAVA_RES = (
    re.compile(r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)"),
    re.compile(r"jvmToolchain\(\s*(\d+)\s*\)"),
    re.compile(r"(?:source|target)Compatibility\s*=\s*(?:JavaVersion\.VERSION_)?['\"]?(1_\d+|\d+(?:\.\d+)?)"),
)


def _scan_dir(path: str, rel: str, req: Requirements) -> None:
    def src(name: str) -> str:
        return f"{rel}/{name}" if rel else name

    names = set()
    try:
        names = set(os.listdir(path))
    except OSError:
        return
    if "package.json" in names:
        try:
            manifest = json.loads(_read(os.path.join(path, "package.json")) or "{}")
        except ValueError:
            manifest = {}
        engines = manifest.get("engines") if isinstance(manifest, dict) else None
        if isinstance(engines, dict) and isinstance(engines.get("node"), str):
            req.node.append((src("package.json") + " engines", engines["node"]))
        lock = next((n for n in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")
                     if n in names), "")
        req.npm_dirs.append((path, lock))
    for name in (".nvmrc", ".node-version"):
        if name in names:
            value = _read(os.path.join(path, name)).strip().splitlines()[:1]
            if value and value[0].strip():
                req.node.append((src(name), value[0].strip()))
    if ".tool-versions" in names:
        for line in _read(os.path.join(path, ".tool-versions")).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("nodejs", "node"):
                req.node.append((src(".tool-versions"), parts[1]))
            elif len(parts) >= 2 and parts[0] == "java" and _java_major(parts[1]):
                req.java.append((src(".tool-versions"), _java_major(parts[1])))
    if ".java-version" in names:
        major = _java_major(_read(os.path.join(path, ".java-version")))
        if major:
            req.java.append((src(".java-version"), major))
    if ".sdkmanrc" in names:
        m = re.search(r"^java=(\S+)", _read(os.path.join(path, ".sdkmanrc")), re.M)
        if m and _java_major(m.group(1)):
            req.java.append((src(".sdkmanrc"), _java_major(m.group(1))))
    if "pom.xml" in names:
        pom = _read(os.path.join(path, "pom.xml"))
        req.maven_dirs.append(path)
        majors = [_java_major(v) for _k, v in _POM_JAVA_RE.findall(pom) if not v.startswith("${")]
        majors = [m for m in majors if m]
        if majors:
            req.java.append((src("pom.xml"), max(majors)))
        if "testcontainers" in pom:
            req.testcontainers.append(src("pom.xml"))
    for gradle in ("build.gradle", "build.gradle.kts"):
        if gradle in names:
            text = _read(os.path.join(path, gradle))
            req.gradle_dirs.append(path)
            for pattern in _GRADLE_JAVA_RES:
                m = pattern.search(text)
                if m and _java_major(m.group(1).replace("_", ".")):
                    req.java.append((src(gradle), _java_major(m.group(1).replace("_", "."))))
                    break
            if "testcontainers" in text:
                req.testcontainers.append(src(gradle))


_scan_lock = threading.Lock()
_scan_cache: Dict[str, Tuple[float, Requirements]] = {}


def requirements(workspace: str) -> Requirements:
    """Manifests in the workspace and up to two folders below it."""
    ws = os.path.realpath(workspace)
    now = time.monotonic()
    with _scan_lock:
        cached = _scan_cache.get(ws)
        if cached and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
    req = Requirements()
    queue: List[Tuple[str, str, int]] = [(ws, "", 0)]
    seen = 0
    while queue and seen < _SCAN_MAX_DIRS:
        path, rel, depth = queue.pop(0)
        seen += 1
        _scan_dir(path, rel, req)
        if depth >= _SCAN_DEPTH:
            continue
        try:
            children = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in children:
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    queue.append((entry.path, f"{rel}/{entry.name}" if rel else entry.name, depth + 1))
            except OSError:
                continue
    with _scan_lock:
        _scan_cache[ws] = (now, req)
    return req


# ── choosing ───────────────────────────────────────────────────────────────

@dataclass
class Selection:
    node: Optional[Toolchain] = None      # None: the default Node on PATH
    java: Optional[Toolchain] = None
    maven: Optional[Toolchain] = None
    notes: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)


def _choose_node(req: Requirements, have: List[Toolchain], sel: Selection) -> None:
    specs = [(src, spec) for src, spec in req.node if satisfies((0, 0, 0), spec) is not None]
    unreadable = [f"{src} '{spec}'" for src, spec in req.node if satisfies((0, 0, 0), spec) is None]
    if unreadable:
        sel.notes.append("could not read the Node version in " + ", ".join(unreadable))
    if not specs or not have:
        return
    fits_all = [t for t in have if all(satisfies(t.version, spec) for _s, spec in specs)]
    fits_any = [t for t in have if any(satisfies(t.version, spec) for _s, spec in specs)]
    chosen = (fits_all or fits_any or [None])[-1]
    asked = "; ".join(f"{src} '{spec}'" for src, spec in specs)
    if chosen is None:
        sel.missing.append(f"Node for {asked}: none installed matches (have "
                           + ", ".join(format_version(t.version) for t in have) + ")")
        return
    if not fits_all:
        sel.notes.append(f"the Node versions asked for disagree ({asked}); using {format_version(chosen.version)}")
    sel.notes.append(f"node {format_version(chosen.version)} ({asked})")
    sel.node = None if chosen.system else chosen


def _choose_java(req: Requirements, have: List[Toolchain], sel: Selection) -> None:
    needs_java = bool(req.java or req.maven_dirs or req.gradle_dirs)
    if not needs_java:
        return
    if not have:
        sel.missing.append("a JDK (the project has " + ", ".join(
            [s for s, _m in req.java] or ["a Java build"]) + "; none is installed here)")
        return
    if not req.java:
        sel.java = have[-1]
        sel.notes.append(f"java {sel.java.version[0]} (no Java version declared; the newest installed)")
        return
    wanted = max(m for _s, m in req.java)
    sources = ", ".join(sorted({s for s, _m in req.java}))
    # The same major, else the oldest newer one: a JDK 21 builds release-17 code.
    fits = [t for t in have if t.version[0] == wanted] or [t for t in have if t.version[0] > wanted]
    chosen = fits[0] if fits else None
    if chosen is None:
        sel.missing.append(f"Java {wanted} ({sources}): not installed (have "
                           + ", ".join(str(t.version[0]) for t in have) + ")")
        return
    sel.java = chosen
    sel.notes.append(f"java {chosen.version[0]} ({sources} asks for {wanted})")


def select(workspace: Optional[str]) -> Selection:
    sel = Selection()
    if not workspace or not os.path.isdir(workspace):
        return sel
    try:
        req = requirements(workspace)
        have = installed()
    except Exception:  # noqa: BLE001 - a toolchain hint must never break a shell
        logger.debug("toolchain selection failed for %s", workspace, exc_info=True)
        return sel
    _choose_node(req, have["node"], sel)
    _choose_java(req, have["java"], sel)
    if req.maven_dirs and have["maven"]:
        sel.maven = have["maven"][-1]
    return sel


def shell_env(workspace: Optional[str], path: Optional[str]) -> Dict[str, str]:
    """PATH (and JAVA_HOME) for a shell working in ``workspace``."""
    sel = select(workspace)
    front = [t.bin for t in (sel.node, sel.java) if t is not None]
    back = [sel.maven.bin] if sel.maven is not None else []
    out: Dict[str, str] = {}
    if front or back:
        parts = [p for p in str(path or "").split(os.pathsep) if p]
        parts = front + [p for p in parts if p not in front and p not in back] + back
        out["PATH"] = os.pathsep.join(parts)
    if sel.java is not None:
        out["JAVA_HOME"] = sel.java.home
    return out


def describe(workspace: Optional[str]) -> str:
    """One line for the agent: which toolchains the shell uses here, and what is missing."""
    sel = select(workspace)
    parts = list(sel.notes)
    if sel.missing:
        parts.append("missing: " + "; ".join(sel.missing))
    if not parts:
        return ""
    have = installed()
    others = ", ".join(t.label() for kind in ("node", "java") for t in have[kind])
    return ("Toolchains for this workspace: " + "; ".join(parts)
            + (f". Installed: {others}; another is on PATH with "
               f"`export PATH={root()}/node/<major>/bin:$PATH` (or java/<major>/bin plus JAVA_HOME)."
               if others else "."))


# ── what a fresh checkout needs ────────────────────────────────────────────

def setup_plan(workspace: Optional[str]) -> Dict[str, object]:
    """Install and test commands for a checkout, in the order to run them.

    Only commands: they run in the agent's (sandboxed) shell, never here --
    install scripts are the project's code.
    """
    if not workspace or not os.path.isdir(workspace):
        return {}
    try:
        req = requirements(workspace)
    except Exception:  # noqa: BLE001
        return {}
    install: List[str] = []
    test: List[str] = []
    notes: List[str] = []
    for path, lock in req.npm_dirs:
        manifest = {}
        try:
            manifest = json.loads(_read(os.path.join(path, "package.json")) or "{}")
        except ValueError:
            pass
        scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
        if not lock and not (isinstance(scripts, dict) and scripts):
            continue
        if not os.path.isdir(os.path.join(path, "node_modules")):
            if lock in ("package-lock.json", "npm-shrinkwrap.json"):
                install.append(f"cd {path} && npm ci --prefer-offline")
            elif lock == "yarn.lock":
                install.append(f"cd {path} && npx --yes yarn@1 install --frozen-lockfile")
            elif lock == "pnpm-lock.yaml":
                install.append(f"cd {path} && npx --yes pnpm install --frozen-lockfile")
            else:
                install.append(f"cd {path} && npm install")
        if isinstance(scripts, dict):
            for name in ("test", "typecheck", "lint"):
                if name in scripts:
                    test.append(f"cd {path} && npm {'test' if name == 'test' else 'run ' + name}")
    for path in req.maven_dirs:
        runner = "./mvnw" if os.path.isfile(os.path.join(path, "mvnw")) else "mvn"
        test.append(f"cd {path} && {runner} test")
    for path in req.gradle_dirs:
        runner = "./gradlew" if os.path.isfile(os.path.join(path, "gradlew")) else "gradle"
        test.append(f"cd {path} && {runner} test")
    if req.testcontainers:
        notes.append("Testcontainers tests (" + ", ".join(req.testcontainers) + ") need Docker, which "
                     "the sandboxed shell does not have: run the unit tests here and leave the "
                     "integration profile to CI.")
    toolchains = describe(workspace)
    if not (install or test or notes or toolchains):
        return {}
    plan: Dict[str, object] = {}
    if install:
        plan["install_first"] = install
    if test:
        plan["tests"] = test
    if toolchains:
        plan["toolchains"] = toolchains
    if notes:
        plan["notes"] = notes
    return plan


def reset_caches_for_tests() -> None:
    with _installed_lock:
        _installed_cache.clear()
    with _scan_lock:
        _scan_cache.clear()
