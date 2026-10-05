"""A local Windows Cookbook job publishes its process id before the runner may replace it.

The detached runner (Git Bash) swaps the pid file for its own Win32 pid once
the .pid.ready marker appears. If Odysseus touched the marker before it wrote
the launcher's pid, the runner's pid was overwritten by the stale fallback and
the status poller watched the wrong process. The runner's first line must be
the pid-record helper's, which maps bash's pid to the Win32 one.
"""
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from routes import cookbook_routes


@pytest.fixture
def windows_download(api, monkeypatch, tmp_path):
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook_routes, "find_bash", lambda: "bash")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=4242))

    touches = []
    real_touch = pathlib.Path.touch

    def touch(self, *args, **kwargs):
        pid_file = self.with_name(self.name.removesuffix(".ready"))
        touches.append((self.name, pid_file.read_text(encoding="utf-8") if pid_file.exists() else None))
        return real_touch(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "touch", touch)

    def download():
        response = api.as_admin().post("/api/model/download", json={"repo_id": "org/model"})
        assert response.status_code == 200 and response.json().get("ok"), response.text
        return response.json()["session_id"]

    download.touches = touches
    return download


def test_the_fallback_pid_is_written_before_the_ready_marker(windows_download, tmp_path):
    session = windows_download()

    assert windows_download.touches == [(f"{session}.pid.ready", "4242")]
    assert (tmp_path / f"{session}.pid").read_text(encoding="utf-8") == "4242"


def test_the_runner_starts_with_the_pid_record_line(windows_download, tmp_path):
    session = windows_download()

    runner = (tmp_path / f"{session}_run.sh").read_text(encoding="utf-8")
    expected = cookbook_routes._windows_local_pid_record_line(
        tmp_path / f"{session}.pid", tmp_path / f"{session}.pid.ready")
    assert runner.splitlines()[0] == expected
