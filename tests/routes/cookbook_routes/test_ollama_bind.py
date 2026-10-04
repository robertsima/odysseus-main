"""The cookbook's Ollama runner listens on loopback unless Ollama runs on another host.

Ollama has no authentication. A runner that bound 0.0.0.0 on this machine
would hand the model API to the whole network; on a remote GPU host it has
to, so Odysseus can reach it, and the runner says so.
"""
import asyncio
import re
import subprocess
from types import SimpleNamespace

import pytest

from routes import cookbook_routes, model_routes

pytestmark = pytest.mark.security


@pytest.fixture
def runners(api, monkeypatch, tmp_path):
    """Serve without launching anything; return the runner script written."""
    async def create_subprocess_shell(cmd, **kwargs):
        class Proc:
            returncode = 0

            async def wait(self):
                return 0

        return Proc()

    async def binary_available(*args, **kwargs):
        return True

    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook_routes, "_binary_available", binary_available)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", create_subprocess_shell)
    # On Windows a local serve is started with Popen instead of tmux.
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=4242))
    # The new endpoint is registered by probing it; nothing is listening.
    monkeypatch.setattr(model_routes, "_probe_endpoint", lambda *args, **kwargs: [])

    def serve(**body):
        response = api.as_admin().post("/api/model/serve", json={"repo_id": "ollama", "cmd": "ollama serve", **body})
        assert response.status_code == 200 and response.json().get("ok"), response.text
        [script] = tmp_path.glob("*_run.sh")
        return script.read_text(encoding="utf-8")

    return serve


def _bind_host(script):
    match = re.search(r"^ODYSSEUS_OLLAMA_HOST='?([^'\s]+)'?$", script, re.MULTILINE)
    assert match, "the runner sets no ODYSSEUS_OLLAMA_HOST"
    return match.group(1)


def test_a_local_ollama_listens_on_loopback(runners):
    script = runners()

    assert _bind_host(script) == "127.0.0.1"
    assert "0.0.0.0" not in script


def test_a_remote_ollama_listens_on_every_interface_and_says_so(runners):
    script = runners(remote_host="gpu-box")

    assert _bind_host(script) == "0.0.0.0"
    assert "WARNING: remote Ollama will bind" in script
