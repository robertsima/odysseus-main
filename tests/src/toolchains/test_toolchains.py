"""Agents can run a project's own tests: its Node and JDK, its dependencies.

On 2026-09-29 Umni's tests could not run from an agent's shell: the backend
needs Java 21 and the image had no JDK; the mobile app needs Node >=24.3 and
the image's only Node was 22; every fresh worktree failed with
"jest: not found"; and each new worker downloaded the whole npm tree again.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src import constants, shell_sandbox as sb, toolchains as tc
from tests import REPO_ROOT


@pytest.fixture(autouse=True)
def _fresh():
    tc.reset_caches_for_tests()
    sb._reset_for_tests()
    yield
    tc.reset_caches_for_tests()
    sb._reset_for_tests()


# ── ranges ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version,spec,expected", [
    ((24, 21, 0), ">=24.3.0 <25", True),
    ((22, 23, 2), ">=24.3.0 <25", False),
    ((25, 0, 0), ">=24.3.0 <25", False),
    ((24, 21, 0), "^24", True),
    ((22, 23, 2), "^20 || ^22", True),
    ((22, 23, 2), "~22.23", True),
    ((22, 24, 0), "~22.23", False),
    ((24, 1, 0), "24.x", True),
    ((24, 1, 0), "24", True),
    ((24, 1, 0), "v24.1.0", True),
    ((20, 0, 0), ">= 18", True),
    ((18, 5, 0), "16 - 18", True),
    ((19, 0, 0), "16 - 18", False),
    ((22, 0, 0), "*", True),
    ((22, 0, 0), ">20.1", True),
    ((20, 1, 5), ">20.1", False),
])
def test_version_ranges(version, spec, expected):
    assert tc.satisfies(version, spec) is expected


def test_unreadable_specs_are_not_guessed():
    assert tc.satisfies((22, 0, 0), "lts/iron") is None


# ── the Umni layout ─────────────────────────────────────────────────────────

def _umni(tmp_path: Path) -> Path:
    repo = tmp_path / "dog-trainer"
    (repo / ".git").mkdir(parents=True)
    (repo / "mobile").mkdir()
    (repo / "mobile" / "package.json").write_text(json.dumps({
        "engines": {"node": ">=24.3.0 <25"},
        "scripts": {"test": "jest --runInBand", "typecheck": "tsc --noEmit"},
    }), encoding="utf-8")
    (repo / "mobile" / "package-lock.json").write_text("{}", encoding="utf-8")
    (repo / "backend").mkdir()
    (repo / "backend" / "pom.xml").write_text(
        "<project><properties><java.version>21</java.version></properties>"
        "<dependency><groupId>org.testcontainers</groupId></dependency></project>", encoding="utf-8")
    (repo / "backend" / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    # Never scanned: dependencies and build output.
    (repo / "mobile" / "node_modules" / "x").mkdir(parents=True)
    (repo / "mobile" / "node_modules" / "x" / "package.json").write_text(
        json.dumps({"engines": {"node": ">=99"}}), encoding="utf-8")
    return repo


def _fake_installed(monkeypatch, tmp_path, *, node=((22, 23, 2, True), (24, 21, 0, False)), java=(21,)):
    found = {"node": [], "java": [], "maven": []}
    for major, minor, patch, system in node:
        home = tmp_path / "tc" / "node" / str(major)
        found["node"].append(tc.Toolchain("node", (major, minor, patch), str(home), system=system))
    for major in java:
        found["java"].append(tc.Toolchain("java", (major, 0, 12), str(tmp_path / "tc" / "java" / str(major))))
    found["maven"].append(tc.Toolchain("maven", (3, 9, 16), str(tmp_path / "tc" / "maven")))
    monkeypatch.setattr(tc, "installed", lambda: found)
    return found


def test_the_umni_manifests_are_read(tmp_path):
    req = tc.requirements(str(_umni(tmp_path)))
    assert req.node == [("mobile/package.json engines", ">=24.3.0 <25")]
    assert req.java == [("backend/pom.xml", 21)]
    assert req.testcontainers == ["backend/pom.xml"]


def test_umni_gets_node_24_and_java_21(tmp_path, monkeypatch):
    repo = _umni(tmp_path)
    _fake_installed(monkeypatch, tmp_path)
    env = tc.shell_env(str(repo), "/usr/local/bin:/usr/bin")
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path / "tc" / "node" / "24" / "bin")
    assert parts[1] == str(tmp_path / "tc" / "java" / "21" / "bin")
    assert parts[-1] == str(tmp_path / "tc" / "maven" / "bin")
    assert env["JAVA_HOME"] == str(tmp_path / "tc" / "java" / "21")
    line = tc.describe(str(repo))
    assert "node 24.21.0" in line and "java 21" in line


def test_nothing_asked_keeps_the_default_node(tmp_path, monkeypatch):
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    _fake_installed(monkeypatch, tmp_path)
    assert tc.shell_env(str(ws), "/usr/bin") == {}


def test_a_version_nothing_installed_meets_is_reported_not_faked(tmp_path, monkeypatch):
    ws = tmp_path / "future"
    ws.mkdir()
    (ws / ".nvmrc").write_text("26\n", encoding="utf-8")
    (ws / "pom.xml").write_text("<java.version>25</java.version>", encoding="utf-8")
    _fake_installed(monkeypatch, tmp_path)
    sel = tc.select(str(ws))
    assert sel.node is None and sel.java is None
    assert any("Node for .nvmrc '26'" in m for m in sel.missing)
    assert any("Java 25 (pom.xml): not installed" in m for m in sel.missing)


def test_an_older_java_release_builds_on_the_nearest_newer_jdk(tmp_path, monkeypatch):
    ws = tmp_path / "legacy"
    ws.mkdir()
    (ws / "build.gradle").write_text("java { sourceCompatibility = JavaVersion.VERSION_17 }", encoding="utf-8")
    _fake_installed(monkeypatch, tmp_path, java=(21, 25))
    assert tc.select(str(ws)).java.version[0] == 21


def test_a_fresh_worktree_is_told_to_install_first(tmp_path, monkeypatch):
    repo = _umni(tmp_path)
    shutil.rmtree(repo / "mobile" / "node_modules")
    _fake_installed(monkeypatch, tmp_path)
    plan = tc.setup_plan(str(repo))
    assert plan["install_first"] == [f"cd {repo / 'mobile'} && npm ci --prefer-offline"]
    assert f"cd {repo / 'mobile'} && npm test" in plan["tests"]
    assert f"cd {repo / 'mobile'} && npm run typecheck" in plan["tests"]
    assert f"cd {repo / 'backend'} && ./mvnw test" in plan["tests"]
    assert "Docker" in plan["notes"][0]
    # Installed already: nothing to install.
    (repo / "mobile" / "node_modules").mkdir()
    tc.reset_caches_for_tests()
    _fake_installed(monkeypatch, tmp_path)
    assert "install_first" not in tc.setup_plan(str(repo))


def test_worktree_start_returns_the_setup(tmp_path, monkeypatch):
    import asyncio

    from src.agent_tools import worktree_tools
    from src.agent_worktree import service

    repo = _umni(tmp_path)
    shutil.rmtree(repo / "mobile" / "node_modules")
    _fake_installed(monkeypatch, tmp_path)

    async def fake_ensure(name, **kwargs):
        return {"path": str(repo), "branch": "agent/umni/x"}

    monkeypatch.setattr(service, "ensure_worktree", fake_ensure)
    result = asyncio.run(worktree_tools.AgentWorktreeTool().execute(
        json.dumps({"action": "start", "name": "x", "repository": str(repo)}), {"owner": "admin"}))
    assert result["exit_code"] == 0, result
    assert result["setup"]["install_first"] and "Run install_first" in result["setup"]["note"]


# ── the sandbox ─────────────────────────────────────────────────────────────

def _flag_pairs(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def _env_of(argv):
    bwrap_at = next(i for i, a in enumerate(argv) if os.path.basename(a).startswith("bwrap"))
    return dict(a.split("=", 1) for a in argv[2:bwrap_at])


def test_the_sandbox_runs_tools_quietly_and_mounts_the_toolchains(tmp_path, monkeypatch):
    tools = tmp_path / "toolchains"
    tools.mkdir()
    monkeypatch.setenv(tc.ROOT_ENV, str(tools))
    argv = sb.build_argv(["true"], workspace=str(tmp_path))
    env = _env_of(argv)
    assert env["CI"] == "true" and env["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"
    assert "--no-transfer-progress" in env["MAVEN_ARGS"]
    assert (os.path.realpath(str(tools)), str(tools)) in _flag_pairs(argv, "--ro-bind")
    # Java reads its home from passwd (/app, hidden in the sandbox), not $HOME:
    # without these Maven and Gradle cached into a folder that vanished.
    assert env["MAVEN_OPTS"] == f"-Duser.home={sb.SANDBOX_HOME}"
    assert env["MAVEN_USER_HOME"] == f"{sb.SANDBOX_HOME}/.m2"
    assert env["GRADLE_USER_HOME"] == f"{sb.SANDBOX_HOME}/.gradle"


def test_the_sandbox_path_follows_the_workspace(tmp_path, monkeypatch):
    repo = _umni(tmp_path)
    _fake_installed(monkeypatch, tmp_path)
    env = _env_of(sb.build_argv(["true"], workspace=str(repo), env={"PATH": "/usr/bin"}))
    assert env["PATH"].startswith(str(tmp_path / "tc" / "node" / "24" / "bin"))
    assert env["JAVA_HOME"] == str(tmp_path / "tc" / "java" / "21")


def test_package_caches_are_per_repository_and_shared_by_its_worktrees(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    main = tmp_path / "dog-trainer"
    (main / ".git" / "worktrees" / "fix").mkdir(parents=True)
    (main / ".git" / "worktrees" / "fix" / "commondir").write_text("../..", encoding="utf-8")
    worktree = tmp_path / "agent_worktrees" / "_repos" / "umni-1" / "fix"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'fix'}", encoding="utf-8")
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)

    assert sb.package_cache_dir(str(worktree)) == sb.package_cache_dir(str(main))
    assert sb.package_cache_dir(str(other)) != sb.package_cache_dir(str(main))

    argv = sb.build_argv(["true"], workspace=str(worktree), package_cache=True)
    binds = dict((dest, src) for src, dest in _flag_pairs(argv, "--bind"))
    for rel in (".npm", ".m2", ".gradle", ".cache"):
        src = binds[f"{sb.SANDBOX_HOME}/{rel}"]
        assert src.startswith(str(tmp_path / "data" / "agent_cache")) and os.path.isdir(src)
    # Only the agent's shells ask for them; turned off, none are bound.
    assert not [d for _s, d in _flag_pairs(sb.build_argv(["true"], workspace=str(main)), "--bind")
                if d.startswith(sb.SANDBOX_HOME)]
    monkeypatch.setattr(sb, "package_cache_enabled", lambda: False)
    assert not [d for _s, d in _flag_pairs(sb.build_argv(["true"], workspace=str(main), package_cache=True),
                                           "--bind") if d.startswith(sb.SANDBOX_HOME)]


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="needs Linux with bubblewrap")
def test_real_sandbox_keeps_the_cache_and_finds_the_toolchain(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "network_enabled", lambda: False)
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    tools = tmp_path / "toolchains"
    node = tools / "node" / "24" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\necho v24.21.0\n", encoding="utf-8")
    node.chmod(node.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(tc.ROOT_ENV, str(tools))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".nvmrc").write_text("24\n", encoding="utf-8")

    def run(script):
        argv = sb.build_argv(["/bin/sh", "-c", script], workspace=str(ws), package_cache=True)
        return subprocess.run(argv, capture_output=True, text=True, timeout=20)

    first = run('node --version; echo cached > "$HOME/.npm/marker"')
    if first.returncode != 0 and "namespace" in (first.stderr or "").lower():
        pytest.skip("user namespaces are not permitted here: " + first.stderr.strip())
    assert "v24.21.0" in first.stdout, first.stderr
    second = run('cat "$HOME/.npm/marker"')
    assert "cached" in second.stdout, second.stderr


# ── the image ───────────────────────────────────────────────────────────────

def test_the_image_carries_pinned_toolchains():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for stage in ("FROM node:24.", "FROM eclipse-temurin:21.", "FROM maven:3.9."):
        line = next(ln for ln in dockerfile.splitlines() if ln.startswith(stage))
        assert "latest" not in line and ":" in line.split()[1]
    assert "/opt/toolchains/node/24/bin/node" in dockerfile
    assert "/opt/toolchains/java/21/" in dockerfile
    assert "/opt/toolchains/maven/" in dockerfile
    assert "/opt/toolchains/java/21/bin/java -version" in dockerfile
