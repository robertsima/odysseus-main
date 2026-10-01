"""The sandbox probe tells a Docker /proc mask from a namespace refusal, runs
degraded without a procfs, reports itself to the settings page, and binds the
workspace repository's managed worktrees (2026-09-28).

The live log read "bwrap: Can't mount proc on /proc: Operation not permitted"
behind a message blaming seccomp/AppArmor, which were already unconfined (the
namespaces had been created). The probe runner is mocked here, so these run
on any platform.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import shell_sandbox as sb

PROC_ERROR = "bwrap: Can't mount proc on /newroot/proc: Operation not permitted"
USERNS_ERROR = ("bwrap: No permissions to creating new namespace, likely because the kernel "
                "does not allow non-privileged user namespaces.")


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    sb._reset_for_tests()
    monkeypatch.setattr(sb, "enabled", lambda: True)
    monkeypatch.setattr(sb, "_platform_problem", lambda: None)
    yield
    sb._reset_for_tests()


def _runner(monkeypatch, respond):
    """Replace the bwrap runner; ``respond(argv)`` returns (code, last line)."""
    calls = []

    def fake(argv):
        calls.append(list(argv))
        return respond(argv)

    monkeypatch.setattr(sb, "_run_bwrap", fake)
    return calls


def _proc_sources(argv):
    """Every host path bound into the sandbox at /proc (or below it)."""
    out = []
    for i, arg in enumerate(argv):
        if arg in ("--bind", "--ro-bind", "--dev-bind", "--bind-try", "--ro-bind-try"):
            src, dest = argv[i + 1], argv[i + 2]
            if src == "/proc" or src.startswith("/proc/") or dest == "/proc" or dest.startswith("/proc/"):
                out.append((arg, src, dest))
    return out


def _script(argv):
    return argv[-1]


def test_proc_mount_failure_falls_back_to_an_empty_proc(monkeypatch, tmp_path):
    calls = _runner(monkeypatch, lambda argv: (1, PROC_ERROR) if "--proc" in argv else (0, ""))

    state = sb.status(refresh=True)

    assert state["available"] is True and state["degraded"] is True
    assert state["reason"].startswith(sb.DEGRADED_REASON)
    assert "systempaths=unconfined" in state["reason"]
    assert len(calls) == 2
    retry = calls[1]
    assert "--proc" not in retry
    assert ["--dir", "/proc"] == retry[retry.index("/proc") - 1: retry.index("/proc") + 1]
    # The degraded probe must not require /proc/self; it checks nothing shows through.
    assert "test -d /proc/self" not in _script(retry) and "! test -e /proc/1" in _script(retry)
    # Commands built after the probe follow it.
    argv = sb.build_argv(["true"], workspace=str(tmp_path))
    assert "--proc" not in argv and ["--dir", "/proc"] == argv[argv.index("/proc") - 1: argv.index("/proc") + 1]
    assert _proc_sources(argv) == []


def test_degraded_is_still_usable_for_a_workspace(monkeypatch, tmp_path):
    _runner(monkeypatch, lambda argv: (1, PROC_ERROR) if "--proc" in argv else (0, ""))
    monkeypatch.setattr(sb, "workspace_problem", lambda ws, **_kw: None)

    assert sb.unavailable_reason(str(tmp_path)) == ""
    assert sb.usable_for(str(tmp_path)) is True


def test_userns_failure_is_unavailable_with_the_seccomp_message(monkeypatch):
    calls = _runner(monkeypatch, lambda argv: (1, USERNS_ERROR))

    state = sb.status(refresh=True)

    assert state["available"] is False and state["degraded"] is False
    reason = state["reason"]
    assert "user namespace" in reason
    assert "seccomp=unconfined" in reason and "apparmor=unconfined" in reason
    assert "systempaths" not in reason, "a namespace refusal is not a /proc mask"
    assert len(calls) == 1, "no degraded retry when namespaces themselves are refused"


def test_proc_failure_that_the_retry_cannot_fix_names_the_compose_line(monkeypatch):
    _runner(monkeypatch, lambda argv: (1, PROC_ERROR) if "--proc" in argv else (1, "bwrap: Failed to mount tmpfs: Operation not permitted"))

    state = sb.status(refresh=True)

    assert state["available"] is False and state["degraded"] is False
    assert "bwrap failed" in state["reason"]


def test_full_procfs_when_the_probe_mounts_it(monkeypatch, tmp_path):
    calls = _runner(monkeypatch, lambda argv: (0, ""))

    state = sb.status(refresh=True)

    assert state == {"enabled": True, "available": True, "degraded": False, "reason": ""}
    assert "test -d /proc/self" in _script(calls[0])
    argv = sb.build_argv(["true"], workspace=str(tmp_path))
    assert ["--proc", "/proc"] == argv[argv.index("/proc") - 1: argv.index("/proc") + 1]


@pytest.mark.parametrize("procfs", [True, False])
def test_command_never_binds_the_containers_proc(procfs, tmp_path):
    argv = sb.build_argv(["/bin/bash", "-c", "ps"], workspace=str(tmp_path), procfs=procfs,
                         extra_ro_binds={str(tmp_path / "job.sh"): "/tmp/.job.sh"})
    assert _proc_sources(argv) == []
    assert not any(a == "--ro-bind" and argv[i + 1] == "/proc" for i, a in enumerate(argv))


def test_the_probe_is_cached(monkeypatch):
    calls = _runner(monkeypatch, lambda argv: (0, ""))
    sb.status()
    sb.status()
    assert len(calls) == 1


# ── the settings route ───────────────────────────────────────────────────

def _app(admin=True):
    from routes import shell_sandbox_routes

    app = FastAPI()
    app.include_router(shell_sandbox_routes.setup_shell_sandbox_routes())
    if admin:
        # Override the object the route module actually depends on: another
        # test reloading core.middleware leaves a different `require_admin`
        # there, and overriding that one let the real admin check run (the
        # full Linux suite then got {"detail": ...} back).
        app.dependency_overrides[shell_sandbox_routes.require_admin] = lambda: None
    return app


def test_route_reports_mode_network_and_probe(monkeypatch):
    _runner(monkeypatch, lambda argv: (1, PROC_ERROR) if "--proc" in argv else (0, ""))
    monkeypatch.setattr(sb, "mode", lambda: "auto")
    monkeypatch.setattr(sb, "network_enabled", lambda: False)

    body = TestClient(_app()).get("/api/settings/shell-sandbox").json()

    assert set(body) == {"mode", "network", "available", "degraded", "reason"}
    assert body["mode"] == "auto" and body["network"] is False
    assert body["available"] is True and body["degraded"] is True
    assert body["reason"].startswith(sb.DEGRADED_REASON)


def test_route_reason_is_null_when_healthy(monkeypatch):
    _runner(monkeypatch, lambda argv: (0, ""))
    monkeypatch.setattr(sb, "mode", lambda: "auto")

    body = TestClient(_app()).get("/api/settings/shell-sandbox").json()

    assert body["available"] is True and body["degraded"] is False and body["reason"] is None


def test_route_is_admin_only(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    resp = TestClient(_app(admin=False)).get("/api/settings/shell-sandbox")
    assert resp.status_code == 403


def test_route_is_registered_in_the_app():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "setup_shell_sandbox_routes()" in source


# ── managed worktrees of the workspace's repository ─────────────────────

_GIT = shutil.which("git")


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "init.defaultBranch=main", *args],
                   cwd=str(cwd), check=True, capture_output=True, text=True)


def _repo(path):
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    (path / "README.md").write_text("x", encoding="utf-8")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


def _binds(argv):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--bind"]


@pytest.fixture
def worktree_layout(tmp_path, monkeypatch):
    if not _GIT:
        pytest.skip("needs git")
    from src.agent_worktree import ownership

    root = (tmp_path / "agent_worktrees").resolve()
    root.mkdir()
    monkeypatch.setattr(ownership, "managed_worktree_root", lambda: root)
    monkeypatch.setattr(sb, "workspace_problem", lambda ws, **_kw: None)
    ws = _repo(tmp_path / "development" / "dog-trainer").resolve()
    other = _repo(tmp_path / "development" / "other").resolve()
    return root, ws, other


def test_workspace_worktree_under_the_managed_root_is_bound(worktree_layout):
    root, ws, _other = worktree_layout
    tree = root / "_repos" / "dog-trainer-50d9" / "bounded-slice"
    _git("worktree", "add", "-q", "-b", "slice", str(tree), cwd=ws)

    argv = sb.build_argv(["true"], workspace=str(ws), procfs=True)

    expected = os.path.normpath(str(tree))
    assert (expected, expected) in _binds(argv)
    assert argv[argv.index("--chdir") + 1] == os.path.realpath(str(ws)), "chdir stays at the workspace"


def test_another_repositorys_worktree_is_not_bound(worktree_layout):
    root, ws, other = worktree_layout
    foreign = root / "_repos" / "other-1" / "slice"
    _git("worktree", "add", "-q", "-b", "slice", str(foreign), cwd=other)

    argv = sb.build_argv(["true"], workspace=str(ws), procfs=True)

    assert _binds(argv) == [(os.path.realpath(str(ws)),) * 2]


def test_worktree_outside_the_managed_root_is_not_bound(worktree_layout, tmp_path):
    _root, ws, _other = worktree_layout
    _git("worktree", "add", "-q", "-b", "stray", str(tmp_path / "elsewhere" / "stray"), cwd=ws)

    argv = sb.build_argv(["true"], workspace=str(ws), procfs=True)

    assert _binds(argv) == [(os.path.realpath(str(ws)),) * 2]


def test_worktree_reached_through_a_symlink_is_not_bound(worktree_layout):
    root, ws, _other = worktree_layout
    tree = root / "_repos" / "dog-trainer-50d9" / "real"
    _git("worktree", "add", "-q", "-b", "real", str(tree), cwd=ws)
    alias = root / "_repos" / "alias"
    try:
        os.symlink(str(tree), str(alias), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    # Point git's record of the worktree at the symlinked path.
    (gitdir_file,) = (ws / ".git" / "worktrees").glob("*/gitdir")
    gitdir_file.write_text(str(alias / ".git") + "\n", encoding="utf-8")

    argv = sb.build_argv(["true"], workspace=str(ws), procfs=True)

    assert _binds(argv) == [(os.path.realpath(str(ws)),) * 2]


def test_worktree_failing_the_workspace_checks_is_not_bound(worktree_layout, monkeypatch):
    root, ws, _other = worktree_layout
    tree = root / "_repos" / "dog-trainer-50d9" / "vaultish"
    _git("worktree", "add", "-q", "-b", "vaultish", str(tree), cwd=ws)
    monkeypatch.setattr(sb, "workspace_problem", lambda path, **_kw: "the workspace overlaps the document vault")

    assert sb.workspace_worktrees(str(ws)) == []


def test_probe_does_not_enumerate_worktrees(monkeypatch):
    monkeypatch.setattr(sb, "workspace_worktrees", lambda ws: pytest.fail("probe enumerated worktrees"))
    _runner(monkeypatch, lambda argv: (0, ""))
    assert sb.status(refresh=True)["available"] is True
