"""File tools must resolve and confine paths off the event loop.

Production blocked the loop for 2.1 to 5.95s inside ``os.path.realpath`` <-
``_agent_readable_data_subdirs_uncached`` <- ``_policy_memo``: read_file,
write_file, edit_file, ls, glob and grep resolved the model's path on the loop,
and every resolution runs realpath over each data subdirectory (slow on a NAS
data folder). The resolution now runs on a worker thread; the policy itself is
unchanged.
"""
import asyncio
import json
import os
import threading
import time

import pytest

from src.agent_tools.filesystem_tools import (
    EditFileTool,
    GlobTool,
    GrepTool,
    LsTool,
    ReadFileTool,
    WriteFileTool,
)


@pytest.fixture
def workspace_file():
    from src.constants import AGENT_WORKSPACE_DIR
    os.makedirs(AGENT_WORKSPACE_DIR, exist_ok=True)
    path = os.path.join(AGENT_WORKSPACE_DIR, "off-loop-policy.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("hello needle\n")
    yield path
    if os.path.exists(path):
        os.remove(path)


def _slow_on_loop(monkeypatch, delay=0.02):
    """realpath that stalls only when called on the thread running the loop."""
    real = os.path.realpath
    main = threading.main_thread()
    calls = {"on_loop": 0}

    def slow(p, *a, **kw):
        if threading.current_thread() is main:
            calls["on_loop"] += 1
            time.sleep(delay)
        return real(p, *a, **kw)

    monkeypatch.setattr(os.path, "realpath", slow)
    return calls


async def _max_gap_while(coro, interval=0.01):
    gaps = []
    stop = False

    async def beat():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(interval)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(beat())
    await asyncio.sleep(0.05)
    try:
        result = await coro
    finally:
        stop = True
        await task
    return result, max(gaps)


def _calls(path):
    return {
        "read_file": (ReadFileTool(), json.dumps({"path": path})),
        "write_file": (WriteFileTool(), json.dumps({"path": path, "content": "rewritten\n"})),
        "edit_file": (EditFileTool(), json.dumps({"path": path, "old_string": "needle", "new_string": "thread"})),
        "ls": (LsTool(), json.dumps({"path": os.path.dirname(path)})),
        "glob": (GlobTool(), json.dumps({"pattern": "off-loop-*.txt", "path": os.path.dirname(path)})),
        "grep": (GrepTool(), json.dumps({"pattern": "needle", "path": os.path.dirname(path)})),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["read_file", "write_file", "edit_file", "ls", "glob", "grep"])
async def test_path_resolution_does_not_stall_the_loop(monkeypatch, workspace_file, tool):
    instance, content = _calls(workspace_file)[tool]
    _slow_on_loop(monkeypatch)

    result, gap = await _max_gap_while(instance.execute(content, {}))

    assert gap < 0.15, f"{tool} stalled the event loop for {gap:.2f}s"
    assert result.get("exit_code") == 0, result


@pytest.mark.asyncio
async def test_read_file_still_returns_the_content(workspace_file):
    result = await ReadFileTool().execute(json.dumps({"path": workspace_file}), {})
    assert "hello needle" in result["output"]


@pytest.mark.asyncio
@pytest.mark.security
@pytest.mark.parametrize("tool", ["read_file", "write_file", "edit_file"])
async def test_path_outside_the_allowed_roots_is_still_refused(monkeypatch, tool):
    outside = os.path.join(os.path.expanduser("~"), ".ssh", "authorized_keys")
    args = {
        "read_file": {"path": outside},
        "write_file": {"path": outside, "content": "x"},
        "edit_file": {"path": outside, "old_string": "a", "new_string": "b"},
    }[tool]
    instance = {"read_file": ReadFileTool, "write_file": WriteFileTool, "edit_file": EditFileTool}[tool]()

    result = await instance.execute(json.dumps(args), {})

    assert result.get("exit_code") == 1
    assert "error" in result
