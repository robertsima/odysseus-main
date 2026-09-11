"""Claude Code runs report live: stream-json lines become a transcript on the
task record and events on the delegating session's activity feed, and the
run's edits are captured relative to the commit it started from."""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from src import agent_activity as act
from src import constants
from src.agent_tools import claude_code_tools as cct


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    act._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


def _lines(*objs):
    return "\n".join(json.dumps(o) for o in objs)


def test_transcript_parses_stream_json_into_entries_and_events(data_dir):
    t = cct._Transcript("sess1", "claude_code-abc", "alice")
    t.feed(b'not json at all\n')
    t.feed(json.dumps({"type": "system", "subtype": "init", "model": "opus", "tools": ["Read", "Edit"], "cwd": "/r"}).encode() + b"\n")
    t.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "I'll look at the parser first.\nThen fix it."},
        {"type": "tool_use", "id": "toolu_1", "name": "Edit", "input": {"file_path": "src/parser.py", "old_string": "a" * 900, "new_string": "b"}},
    ]}}).encode() + b"\n")
    t.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "The file has been updated."}], "is_error": False},
    ]}}).encode() + b"\n")
    t.feed(json.dumps({"type": "result", "subtype": "success", "result": "Done", "num_turns": 3}).encode() + b"\n")

    kinds = [e["kind"] for e in t.entries]
    assert kinds == ["status", "message", "tool_start", "tool_result"]
    assert t.entries[2]["tool"] == "Edit" and t.entries[2]["summary"] == "src/parser.py"
    assert len(t.entries[2]["input"]["old_string"]) <= 401, "tool inputs are clipped on the record"
    assert t.entries[3]["tool"] == "Edit" and t.entries[3]["excerpt"] == "The file has been updated."
    assert t.envelope["result"] == "Done"

    events = act.history("sess1")
    assert [e["kind"] for e in events] == ["status", "message", "tool_start", "tool_result"]
    assert all(e["source"] == "claude_code" and e["run_id"] == "claude_code-abc" for e in events)
    assert events[2]["title"] == "Edit src/parser.py"


def test_transcript_is_bounded(data_dir, monkeypatch):
    monkeypatch.setattr(cct, "MAX_TRANSCRIPT_ENTRIES", 3)
    t = cct._Transcript(None, "r", None)
    for i in range(6):
        t.feed(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"m{i}"}]}}).encode())
    assert len(t.entries) == 3 and t.truncated is True


def test_build_argv_streams_only_when_asked():
    argv = cct._build_argv(Path("/bin/claude"), "do it", ["Read"], list(cct._OPTIONAL_FLAGS), stream=True)
    assert "stream-json" in argv and "--verbose" in argv
    argv = cct._build_argv(Path("/bin/claude"), "do it", ["Read"], list(cct._OPTIONAL_FLAGS))
    assert "json" in argv and "stream-json" not in argv and "--verbose" not in argv


async def test_binary_info_detects_stream_json(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 9.9.9; else echo '--tools --allowedTools --output-format json|stream-json --verbose --no-session-persistence'; fi\n", encoding="utf-8")
    binary.chmod(0o755)
    info = await cct.binary_info(binary)
    assert info["stream_json"] is True and "--verbose" in info["flags"]


async def test_streamed_run_records_transcript_activity_and_changes(data_dir, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    # A stand-in for `claude -p --output-format stream-json`: it edits a file,
    # commits, and prints the JSONL a real run prints.
    stream = _lines(
        {"type": "system", "subtype": "init", "model": "sonnet", "tools": ["Edit"], "cwd": str(repo)},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "app.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Changed x to 2 and committed."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "x is now 2", "num_turns": 2, "total_cost_usd": 0.02},
    ).replace("'", "'\\''")
    binary = tmp_path / "claude"
    binary.write_text(
        "#!/bin/sh\n"
        "printf 'x = 2\\n' > app.py\n"
        "printf 'y = 3\\n' > new.py\n"
        "git -c user.name=t -c user.email=t@x commit -qam 'claude: bump x' >/dev/null 2>&1\n"
        f"printf '%s\\n' '{stream}'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))

    async def fake_info(b=None):
        return {"path": str(binary), "available": True, "version": "9", "flags": list(cct._OPTIONAL_FLAGS),
                "error": "", "stream_json": True}

    monkeypatch.setattr(cct, "binary_info", fake_info)

    with cct.run_context(session_id="chat-7", owner="alice", label="bump x"):
        out = await cct._run_claude(repo, "bump x", 30, ["Edit"])

    assert out["exit_code"] == 0 and out["result"] == "x is now 2" and out["num_turns"] == 2
    assert [e["kind"] for e in out["transcript"]] == ["status", "tool_start", "tool_result", "message"]
    assert out["start_commit"] and out["commit"] != out["start_commit"]
    assert [c["subject"] for c in out["commits"]] == ["claude: bump x"]
    by_path = {f["path"]: f for f in out["changes"]}
    assert by_path["app.py"]["additions"] == 1 and by_path["app.py"]["deletions"] == 1
    assert by_path["new.py"]["status"] == "untracked"

    events = act.history("chat-7")
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "file_change" in kinds and "commit" in kinds and "tool_start" in kinds
    finished = events[-1]
    assert finished["data"]["status"] == "completed" and finished["data"]["commits"] == 1
    assert finished["title"].startswith("Claude Code: bump x")
    run = act.get_run(events[0]["run_id"])
    assert run["status"] == "completed" and run["session_id"] == "chat-7" and run["owner"] == "alice"


async def test_batch_binary_still_works_and_reports_a_run(data_dir, tmp_path, monkeypatch):
    envelope = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "PONG", "num_turns": 1})
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\nprintf '%s' '{envelope}'\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(cct, "DEFAULT_BINARY", str(binary))

    async def fake_info(b=None):
        return {"path": str(binary), "available": True, "version": "0", "flags": list(cct._OPTIONAL_FLAGS), "error": ""}

    monkeypatch.setattr(cct, "binary_info", fake_info)
    with cct.run_context(session_id="chat-8", owner="alice"):
        out = await cct._run_claude(tmp_path, "ping", 30, ["Read"])
    assert out["result"] == "PONG" and "transcript" not in out
    assert [e["kind"] for e in act.history("chat-8")] == ["run_started", "run_finished"]


async def test_task_runner_threads_the_session_into_the_run(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(cct, "_approved_repository", lambda v: Path(v))
    seen = {}

    async def fake_run_claude(repository, prompt, timeout, tools, on_process=None, model=None):
        seen["ctx"] = dict(cct._RUN_CONTEXT.get())
        return {"exit_code": 0, "result": "ok", "changes": [{"path": "a"}], "commits": [{"sha": "1"}, {"sha": "2"}]}

    monkeypatch.setattr(cct, "_run_claude", fake_run_claude)
    runner = cct.ClaudeCodeTaskRunner(store_path=str(tmp_path / "tasks.json"))
    started = await runner.start({"repository": str(tmp_path), "prompt": "go", "label": "L"}, owner="bob", session_id="chat-9")
    await asyncio.wait_for(runner.jobs[started["task_id"]], timeout=5) if started["task_id"] in runner.jobs else None
    for _ in range(50):
        if runner.get(started["task_id"])["status"] != "running":
            break
        await asyncio.sleep(0.02)
    assert seen["ctx"]["session_id"] == "chat-9" and seen["ctx"]["owner"] == "bob"
    assert seen["ctx"]["task_id"] == started["task_id"] and seen["ctx"]["label"] == "L"
    row = runner.summaries()[0]
    assert row["session_id"] == "chat-9" and row["changes_count"] == 1 and row["commit_count"] == 2
