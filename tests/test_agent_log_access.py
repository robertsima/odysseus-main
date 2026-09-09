"""Read-only application-log access for the agent.

Two properties matter: the agent can only reach known log directories by file
name, and anything credential-shaped is redacted before it reaches the model.
"""

import os

import pytest

from src import agent_logs

pytestmark = pytest.mark.area_security


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    directory.mkdir()
    monkeypatch.setattr(agent_logs, "log_roots", lambda: [str(directory)])
    return directory


def _write(directory, name, lines):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def test_lists_only_log_files(log_dir):
    _write(log_dir, "app.log", ["a"])
    _write(log_dir, "app.log.1", ["b"])
    _write(log_dir, "notes.txt", ["c"])
    names = {entry["name"] for entry in agent_logs.logs_index()}
    assert names == {"app.log", "app.log.1"}


def test_defaults_to_the_application_log(log_dir):
    _write(log_dir, "serve.log", ["x"])
    _write(log_dir, "app.log", ["hello"])
    assert agent_logs.read_log()["name"] == "app.log"


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "/etc/passwd", "..", "logs/app.log", "sub\\app.log"],
)
def test_a_path_is_never_accepted_as_a_log_name(log_dir, name):
    _write(log_dir, "app.log", ["hello"])
    with pytest.raises(RuntimeError, match="no log named"):
        agent_logs.read_log(name)


def test_tail_returns_the_last_lines(log_dir):
    _write(log_dir, "app.log", [f"line {i}" for i in range(50)])
    result = agent_logs.read_log("app.log", lines=5)
    assert result["line_count"] == 5
    assert result["lines"][-1] == "line 49"


def test_line_count_is_clamped(log_dir):
    _write(log_dir, "app.log", [f"line {i}" for i in range(10)])
    assert agent_logs.read_log("app.log", lines=10_000)["line_count"] == 10
    assert agent_logs.read_log("app.log", lines=0)["line_count"] == 1


def test_substring_filter_searches_the_whole_tail_not_just_the_window(log_dir):
    lines = [f"noise {i}" for i in range(300)]
    lines.insert(0, "the needle is here")
    _write(log_dir, "app.log", lines)
    result = agent_logs.read_log("app.log", lines=10, contains="needle")
    assert result["line_count"] == 1
    assert "needle" in result["lines"][0]


def test_level_filter_includes_more_severe_levels(log_dir):
    _write(log_dir, "app.log", [
        "2026-01-01 - x - DEBUG - quiet",
        "2026-01-01 - x - INFO - normal",
        "2026-01-01 - x - WARNING - hmm",
        "2026-01-01 - x - ERROR - broken",
    ])
    result = agent_logs.read_log("app.log", level="WARNING")
    assert result["line_count"] == 2
    assert all("DEBUG" not in ln and "INFO" not in ln for ln in result["lines"])


def test_unknown_level_is_rejected(log_dir):
    _write(log_dir, "app.log", ["x"])
    with pytest.raises(RuntimeError, match="level must be one of"):
        agent_logs.read_log("app.log", level="LOUD")


@pytest.mark.parametrize(
    "line,secret",
    [
        ("connecting to https://user:hunter2@example.com/v1", "hunter2"),
        ("GET https://api.example.com/v1?api_key=abcdef123456", "abcdef123456"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ('{"api_key": "sk-livekey12345678"}', "sk-livekey12345678"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("installation ghs_abcdefghijklmnopqrstuvwxyz0123456789", "ghs_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        ("password=supersecretvalue", "supersecretvalue"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
         "eyJhbGciOiJIUzI1NiJ9"),
    ],
)
def test_credentials_are_redacted_from_returned_lines(log_dir, line, secret):
    _write(log_dir, "app.log", [line])
    out = agent_logs.read_log("app.log")["lines"][0]
    assert secret not in out


def test_redaction_keeps_the_line_useful(log_dir):
    _write(log_dir, "app.log", ["ERROR calling https://api.example.com/v1/chat?api_key=zzzz9999"])
    out = agent_logs.read_log("app.log")["lines"][0]
    assert "api.example.com" in out and "/v1/chat" in out
    assert "zzzz9999" not in out


def test_symlinked_log_is_ignored(log_dir, tmp_path):
    secret = tmp_path / "outside.log"
    secret.write_text("private\n")
    os.symlink(str(secret), os.path.join(str(log_dir), "sneaky.log"))
    assert "sneaky.log" not in {entry["name"] for entry in agent_logs.logs_index()}


def test_missing_log_directory_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_logs, "log_roots", lambda: [str(tmp_path / "nope")])
    assert agent_logs.logs_index() == []
    with pytest.raises(RuntimeError, match="no log named"):
        agent_logs.read_log("app.log")
