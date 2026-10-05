import subprocess
from pathlib import Path

from core.platform_compat import find_bash
from tests import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check-docker-amd-gpu.sh"
# A bare "bash" on Windows can resolve to the WSL launcher in System32, which
# cannot read a Windows path. find_bash picks the bash the product uses, and
# that bash reads forward-slash paths on every platform.
BASH = find_bash() or "bash"


def test_amd_gpu_check_rejects_unknown_extra_arg_before_diagnostics():
    proc = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--bad-option"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "Unknown option: --bad-option" in proc.stderr


def test_amd_gpu_check_shell_syntax():
    subprocess.run([BASH, "-n", SCRIPT.as_posix()], check=True)
