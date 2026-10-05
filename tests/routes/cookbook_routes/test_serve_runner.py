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


# vLLM versions differ on --swap-space. The runner asks the installed vllm
# whether it knows the flag: when it does, the serve gets `--swap-space 0`
# (no CPU swap reserved per GPU); when it does not, a flag the caller passed
# is dropped, or vllm exits on an unknown argument. These tests run the
# runner with a stand-in `vllm` and a stand-in vllm Python package that
# record how the server is finally started.
REAL_POPEN = subprocess.Popen

VLLM_STUB = """#!/bin/bash
if [ "$2" = "--help" ] || [ "$3" = "--help" ]; then
  echo "usage: vllm serve [--port PORT] $VLLM_STUB_HELP_FLAGS"
  exit 0
fi
echo "$@" >> "$VLLM_STUB_RECORD"
"""

VLLM_PACKAGE = {
    "vllm/__init__.py": "",
    "vllm/engine/__init__.py": "",
    "vllm/engine/arg_utils.py": (
        "class EngineArgs:\n"
        "    def __init__(self, swap_space=4):\n        self.swap_space = swap_space\n"
        "    def create_engine_config(self):\n        return self.swap_space\n"
        "class AsyncEngineArgs(EngineArgs):\n    pass\n"
    ),
    "vllm/config.py": "class CacheConfig:\n    swap_space = 4\n",
    "vllm/entrypoints/__init__.py": "",
    "vllm/entrypoints/cli/__init__.py": "",
    "vllm/entrypoints/cli/main.py": (
        "import os, sys\n"
        "def main():\n"
        "    open(os.environ['VLLM_STUB_RECORD'], 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "    return 0\n"
    ),
}


def _start_vllm(launch, tmp_path, *, help_flags, cmd):
    """Run the runner for `cmd` and return the lines the stand-ins recorded."""
    import os
    import shutil
    import sys

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("needs bash")
    script = launch("/api/model/serve", repo_id="org/m", cmd=cmd)
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    stubs = tmp_path / "stubs"
    for directory in (home, bin_dir, stubs):
        directory.mkdir()
    (bin_dir / "vllm").write_text(VLLM_STUB, encoding="utf-8", newline="\n")
    # The runner calls python3; use the interpreter running the tests.
    (bin_dir / "python3").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8", newline="\n")
    for name in ("vllm", "python3"):
        (bin_dir / name).chmod(0o755)
    for rel, text in VLLM_PACKAGE.items():
        path = stubs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    record = tmp_path / "record.txt"
    runner = tmp_path / "runner.sh"
    runner.write_text(script, encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(stubs),
        "VLLM_STUB_RECORD": str(record),
        "VLLM_STUB_HELP_FLAGS": help_flags,
    }
    # `launch` replaces subprocess.Popen so the route starts nothing; the
    # runner itself needs the real one.
    process = REAL_POPEN([bash, str(runner)], env=env, cwd=tmp_path, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    process.communicate(timeout=60)
    return record.read_text(encoding="utf-8").splitlines() if record.exists() else []


def test_a_vllm_that_knows_swap_space_is_started_with_it_set_to_zero(launch, tmp_path):
    started = _start_vllm(launch, tmp_path, help_flags="[--swap-space GiB]", cmd="vllm serve org/m --port 8000")
    assert started == ["serve org/m --port 8000 --swap-space 0"]


def test_a_vllm_without_swap_space_is_started_without_the_flag(launch, tmp_path):
    started = _start_vllm(launch, tmp_path, help_flags="", cmd="vllm serve org/m --port 8000 --swap-space 8")
    [args] = started
    assert "--swap-space" not in args
    assert args.startswith("serve org/m --port 8000")


def test_a_serve_off_the_local_windows_host_keeps_its_env_prefix_as_given(launch):
    # Only the local Git Bash runner needs the PowerShell activation turned
    # into a bash one; every other runner gets the prefix untouched.
    script = launch("/api/model/serve", repo_id="org/m-GGUF", cmd=LLAMA_SERVER, env_prefix=WINDOWS_VENV)

    assert "Activate.ps1" in script
    assert 'source "/c/Users' not in script
