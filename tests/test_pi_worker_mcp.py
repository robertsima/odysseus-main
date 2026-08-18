import json

import pytest

from mcp_servers import pi_worker_server as worker


class _FakeReader:
    def __init__(self, lines):
        self._lines = [json.dumps(line).encode() + b"\n" for line in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""

    async def read(self):
        return b""


class _FakeWriter:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, events):
        self.stdin = _FakeWriter()
        self.stdout = _FakeReader(events)
        self.stderr = _FakeReader([])
        self.returncode = None
        self.killed = False

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_project_path_is_confined_to_development(monkeypatch):
    monkeypatch.setenv(worker.ROOT_ENV, "D:/Development")
    assert worker._validate_project_path(r"D:\Development\repo") == "D:/Development/repo"

    with pytest.raises(ValueError, match="must stay under"):
        worker._validate_project_path(r"C:\Users\rober")

    with pytest.raises(ValueError):
        worker._validate_project_path(r"D:\Development\..\Windows")


def test_ssh_command_uses_rpc_worker_without_task_in_command(monkeypatch):
    monkeypatch.setenv(worker.HOST_ENV, "rober@192.168.1.132")
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/ssh")

    argv = worker._ssh_argv("D:/Development/example project", "high", True)

    assert argv[0] == "/usr/bin/ssh"
    assert argv[-2] == "rober@192.168.1.132"
    assert "Start-Pi-Worker.ps1" in argv[-1]
    assert '"D:/Development/example project"' in argv[-1]
    assert "-Rpc" in argv[-1]
    assert "-NoSession" in argv[-1]


def test_append_acceptance_record_writes_only_configured_ai_mind(monkeypatch, tmp_path):
    ai_mind = tmp_path / "AI Mind"
    monkeypatch.setenv(worker.DOC_ROOT_ENV, str(ai_mind))
    monkeypatch.setenv(worker.ROOT_ENV, "D:/Development")

    path = worker._append_acceptance_record({
        "project_path": "D:/Development/example",
        "task": "Repair focused parser bug",
        "outcome": "Handled empty tokens without changing normal parsing.",
        "verification": "pytest tests/test_parser.py -q passed.",
        "files_changed": ["src/parser.py", "tests/test_parser.py"],
        "limitations": "None known.",
    })

    assert path == str(ai_mind / worker.DEFAULT_DOC_NAME)
    note = (ai_mind / worker.DEFAULT_DOC_NAME).read_text(encoding="utf-8")
    assert "Repair focused parser bug" in note
    assert "D:/Development/example" in note
    assert "pytest tests/test_parser.py -q passed" in note


def test_acceptance_record_rejects_non_ai_mind_root(monkeypatch, tmp_path):
    monkeypatch.setenv(worker.DOC_ROOT_ENV, str(tmp_path / "Vault Mind"))
    with pytest.raises(ValueError, match="must point to the AI Mind"):
        worker._documentation_path()


@pytest.mark.asyncio
async def test_tool_list_exposes_delegation_and_acceptance_record():
    tools = await worker.list_tools()
    assert [tool.name for tool in tools] == ["run_pi_task", "record_pi_task"]


@pytest.mark.asyncio
async def test_run_pi_task_returns_final_rpc_text(monkeypatch):
    monkeypatch.setenv(worker.HOST_ENV, "rober@192.168.1.132")
    monkeypatch.setenv(worker.ROOT_ENV, "D:/Development")
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/ssh")

    process = _FakeProcess([
        {"id": "odysseus-prompt", "type": "response", "success": True},
        {"type": "agent_settled"},
        {
            "id": "odysseus-result",
            "type": "response",
            "success": True,
            "data": {"text": "Task completed on Windows."},
        },
    ])

    async def fake_subprocess(*args, **kwargs):
        return process

    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", fake_subprocess)

    result = await worker._run_pi_task(
        "D:/Development/example",
        "Inspect the repository.",
        no_session=True,
        timeout_seconds=30,
    )

    assert result == "Task completed on Windows."
    payloads = [json.loads(raw.decode()) for raw in process.stdin.writes]
    assert payloads == [
        {"type": "prompt", "id": "odysseus-prompt", "message": "Inspect the repository."},
        {"type": "get_last_assistant_text", "id": "odysseus-result"},
    ]
    assert process.stdin.closed is True
    assert process.killed is False
