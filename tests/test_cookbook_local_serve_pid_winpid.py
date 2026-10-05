"""Behavioral regression coverage for Windows-local Cookbook PID recording."""

import os
import subprocess
import time
from pathlib import Path

from core.platform_compat import find_bash
from routes.cookbook_routes import _windows_local_pid_record_line


# The prelude runs under the bash the product launches. On Windows a bare
# "bash" can resolve to the WSL launcher in System32 instead.
BASH = find_bash() or "bash"


def _fake_cat(tmp_path: Path, body: str) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    cat = fake_bin / "cat"
    cat.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    cat.chmod(0o755)
    return fake_bin


def _env_for(fake_bin: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env.update(extra)
    return env


def _run_pid_line(
    pid_path: Path,
    ready_path: Path,
    fake_bin: Path,
    **extra_env: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", _windows_local_pid_record_line(pid_path, ready_path)],
        capture_output=True,
        text=True,
        env=_env_for(fake_bin, **extra_env),
        timeout=10,
    )


def test_windows_local_pid_line_records_numeric_winpid_after_fallback(tmp_path):
    pid_path = tmp_path / "serve.pid"
    ready_path = tmp_path / "serve.pid.ready"

    pid_path.write_text("11111", encoding="utf-8")
    ready_path.touch()

    cat_arg = tmp_path / "cat-arg.txt"
    fake_bin = _fake_cat(
        tmp_path,
        'printf "%s\\n" "$1" > "$FAKE_CAT_ARG"\n'
        'printf "%s\\n" "$FAKE_WINPID"',
    )

    result = _run_pid_line(
        pid_path,
        ready_path,
        fake_bin,
        FAKE_CAT_ARG=str(cat_arg),
        FAKE_WINPID="42324",
    )

    assert result.returncode == 0, result.stderr
    assert pid_path.read_text(encoding="utf-8").strip() == "42324"
    assert not ready_path.exists()

    proc_path = cat_arg.read_text(encoding="utf-8").strip()
    parts = proc_path.strip("/").split("/")
    assert len(parts) == 3
    assert parts[0] == "proc"
    assert parts[1].isdigit()
    assert parts[2] == "winpid"


def test_windows_local_pid_line_waits_for_python_fallback_before_replacing(tmp_path):
    pid_path = tmp_path / "serve.pid"
    ready_path = tmp_path / "serve.pid.ready"

    fake_bin = _fake_cat(
        tmp_path,
        'printf "%s\\n" "$FAKE_WINPID"',
    )

    proc = subprocess.Popen(
        [
            BASH,
            "-c",
            _windows_local_pid_record_line(pid_path, ready_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env_for(fake_bin, FAKE_WINPID="42324"),
    )

    # The inner shell has started, but Python has not published its fallback yet.
    time.sleep(0.05)
    assert proc.poll() is None
    assert not pid_path.exists()

    # Simulate the post-Popen Python publication order.
    pid_path.write_text("31100", encoding="utf-8")
    ready_path.touch()

    stdout, stderr = proc.communicate(timeout=10)

    assert proc.returncode == 0, stderr or stdout
    assert pid_path.read_text(encoding="utf-8").strip() == "42324"
    assert not ready_path.exists()


def test_windows_local_pid_line_preserves_outer_pid_when_mapping_missing(tmp_path):
    pid_path = tmp_path / "serve.pid"
    ready_path = tmp_path / "serve.pid.ready"

    pid_path.write_text("31100", encoding="utf-8")
    ready_path.touch()

    fake_bin = _fake_cat(tmp_path, "exit 1")

    result = _run_pid_line(
        pid_path,
        ready_path,
        fake_bin,
    )

    assert result.returncode == 0, result.stderr
    assert pid_path.read_text(encoding="utf-8").strip() == "31100"
    assert not ready_path.exists()


def test_windows_local_pid_line_rejects_malformed_mapping(tmp_path):
    pid_path = tmp_path / "serve.pid"
    ready_path = tmp_path / "serve.pid.ready"

    pid_path.write_text("31100", encoding="utf-8")
    ready_path.touch()

    fake_bin = _fake_cat(
        tmp_path,
        'printf "not-a-win32-pid\\n"',
    )

    result = _run_pid_line(
        pid_path,
        ready_path,
        fake_bin,
    )

    assert result.returncode == 0, result.stderr
    assert pid_path.read_text(encoding="utf-8").strip() == "31100"
    assert not ready_path.exists()

