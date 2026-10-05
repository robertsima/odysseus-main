"""A bash command that prints nothing for a minute is stopped (src/agent_tools/subprocess_tools.py).

On 2026-09-30 `./mvnw -q -Dtest=... test` hung in an integration test and
printed nothing for 43 minutes; the agent sat on its way to the hour limit
until the user stopped the turn.
"""
import asyncio
import sys

import pytest

from src.agent_tools import subprocess_tools as st
from src.tool_schemas import function_call_to_tool_block
from src.tool_types import ToolBlock, ToolBlockWithOptions


def test_default_is_a_minute_and_a_call_can_ask_for_more(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    assert st.bash_idle_timeout() == 60
    assert st.bash_idle_timeout(300) == 300
    assert st.bash_idle_timeout(3) == 10            # not absurdly short
    assert st.bash_idle_timeout(10 ** 6) == st.DEFAULT_BASH_TIMEOUT
    assert st.bash_idle_timeout(0) is None          # a call may turn it off
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 0 if key == "bash_idle_timeout_seconds" else default)
    assert st.bash_idle_timeout() is None           # so may the setting


def test_the_idle_message_says_why_and_how_to_rerun():
    msg = st._stopped_message("idle", 60.0, fresh_shell=True)
    assert "60s without any new output" in msg
    for hint in ("-q", "idle_timeout", "#!bg", "manage_bg_jobs", "surefire.timeout"):
        assert hint in msg


def test_idle_timeout_travels_with_a_native_bash_call():
    block = function_call_to_tool_block("bash", '{"command": "mvn test", "idle_timeout": 600}')
    assert isinstance(block, ToolBlockWithOptions)
    assert block == ToolBlock("bash", "mvn test")
    assert block.options == {"idle_timeout": 600}
    plain = function_call_to_tool_block("bash", '{"command": "ls"}')
    assert type(plain) is ToolBlock


@pytest.mark.asyncio
async def test_pipe_path_stops_a_silent_process():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _, _rc, stopped = await st._run_subprocess_streaming(proc, timeout=60, idle_timeout=1.0)
    assert stopped == "idle"
    assert out.strip() == "start"


@pytest.mark.asyncio
async def test_pipe_path_lets_a_talkative_process_finish():
    code = "import time\nfor i in range(4):\n    print(i, flush=True); time.sleep(0.4)\n"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _, rc, stopped = await st._run_subprocess_streaming(proc, timeout=60, idle_timeout=1.0)
    assert (rc, stopped) == (0, "")
    assert out.split() == ["0", "1", "2", "3"]
