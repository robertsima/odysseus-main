"""The runner scripts POST /api/model/serve and /api/model/download write.

The route builds a bash runner per launch and starts it in tmux (or, for the
local Windows host, as a detached Git Bash process). These tests launch
through HTTP with nothing actually started and read the runner the route
wrote.
"""
import asyncio
import re
import subprocess
from types import SimpleNamespace

import pytest

from routes import cookbook_routes, model_routes


@pytest.fixture
def launch(api, monkeypatch, tmp_path):
    """POST a launch and return the runner script it wrote."""
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
    # The local Windows host starts the runner with Popen instead of tmux.
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=4242))
    # Serving registers an endpoint by probing it; nothing is listening.
    monkeypatch.setattr(model_routes, "_probe_endpoint", lambda *args, **kwargs: [])

    def run(path, *, local_windows=False, **body):
        monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", local_windows)
        response = api.as_admin().post(path, json=body)
        assert response.status_code == 200 and response.json().get("ok"), response.text
        [script] = tmp_path.glob("*_run.sh")
        return script.read_text(encoding="utf-8")

    return run


def _path_exports(script):
    """The directories each `export PATH=...` line puts in front of $PATH."""
    return [
        m.group(1).split(":")
        for m in re.finditer(r'^export PATH="([^"]*)"$', script, re.MULTILINE)
    ]


LLAMA_SERVER = 'llama-server --model "/models/m.gguf" --host 0.0.0.0 --port 8080 -ngl 99 -c 8192'


def test_local_windows_llama_server_runs_without_building_llama_cpp(launch):
    script = launch("/api/model/serve", local_windows=True, repo_id="org/m-GGUF", cmd=LLAMA_SERVER)

    assert "git clone" not in script
    assert "cmake" not in script
    assert LLAMA_SERVER in script


def test_local_windows_runner_finds_the_user_and_cuda_llama_server_builds(launch):
    script = launch("/api/model/serve", local_windows=True, repo_id="org/m-GGUF", cmd=LLAMA_SERVER)

    dirs = [d for export in _path_exports(script) for d in export]
    assert "$HOME/bin" in dirs
    assert "$HOME/llama.cpp/build-cuda/bin/Release" in dirs
    assert script.index("$HOME/llama.cpp/build-cuda/bin/Release") < script.index(LLAMA_SERVER)


def test_a_linux_llama_server_is_built_when_missing(launch):
    script = launch("/api/model/serve", repo_id="org/m-GGUF", cmd=LLAMA_SERVER)

    assert "command -v llama-server" in script
    assert "git clone" in script


def test_every_llama_cpp_python_install_requests_the_server_extra(launch):
    cmd = "python3 -m llama_cpp.server --model /models/m.gguf --host 0.0.0.0 --port 8000"
    script = launch("/api/model/serve", repo_id="org/m-GGUF", cmd=cmd)

    installs = [
        extra
        for line in script.splitlines()
        if "install" in line and not line.lstrip().startswith(("#", "echo"))
        for extra in re.findall(r"llama-cpp-python(?!/)(\S{0,8})", line)
    ]
    assert installs, "the runner installs llama-cpp-python when it is missing"
    # A bare install passes `import llama_cpp` but lacks the server's
    # dependencies, so `python -m llama_cpp.server` then crashes.
    assert all(extra.startswith("[server]") for extra in installs), installs


def test_a_pip_install_of_llama_cpp_gets_the_server_extra_and_the_cpu_wheel_index(launch):
    script = launch("/api/model/serve", repo_id="llama-cpp-python", cmd="python3 -m pip install llama_cpp")

    [install] = [line for line in script.splitlines() if "pip install" in line and "llama" in line][:1]
    assert "llama-cpp-python[server]" in install
    assert "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu" in install


def test_a_pip_install_keeps_its_own_wheel_index_url_intact(launch):
    index = "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    script = launch(
        "/api/model/serve",
        repo_id="llama-cpp-python",
        cmd=f"python3 -m pip install llama-cpp-python --extra-index-url {index}",
    )

    assert f"--extra-index-url {index}" in script
    assert "llama-cpp-python[server]/whl" not in script
    assert "/whl/cpu" not in script


def test_llama_cpp_python_cache_types_reach_the_runner_as_numbers(launch):
    cmd = (
        "python3 -m llama_cpp.server --model /models/m.gguf --host 0.0.0.0 --port 8000 "
        "--type_k q4_0 --type_v q4_0"
    )
    script = launch("/api/model/serve", repo_id="org/m-GGUF", cmd=cmd)

    # llama-cpp-python takes ggml type ids; q4_0 is 2.
    assert "--type_k 2 --type_v 2" in script
    assert "q4_0" not in script


WINDOWS_VENV = "& 'C:\\Users\\me\\My Envs\\venv\\Scripts\\Activate.ps1'"


def test_a_local_windows_serve_activates_the_venv_the_bash_way(launch):
    script = launch(
        "/api/model/serve", local_windows=True, repo_id="org/m-GGUF", cmd=LLAMA_SERVER, env_prefix=WINDOWS_VENV,
    )

    assert 'source "/c/Users/me/My Envs/venv/Scripts/activate"' in script
    assert "Activate.ps1" not in script


def test_a_local_windows_download_activates_the_venv_the_bash_way(launch):
    script = launch(
        "/api/model/download", local_windows=True, repo_id="org/m-GGUF", backend="vllm", env_prefix=WINDOWS_VENV,
    )

    assert 'source "/c/Users/me/My Envs/venv/Scripts/activate"' in script
    assert "Activate.ps1" not in script
