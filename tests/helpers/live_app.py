"""The real app in a subprocess, with a scripted model, for browser flows.

:class:`MockModel` is a small OpenAI-compatible server on a thread of the test
process. It answers from the request alone: a conversation holding one of the
``PROBE_*`` markers gets that probe's scripted reply, anything else an echo.
Requests without tools (chat titles, memory extraction) get a neutral reply.
A reply can stop at a named gate until the test calls :meth:`MockModel.release`,
so a test can look at the page while the reply is still streaming.

:class:`LiveApp` runs ``python app.py`` on a free loopback port with a scratch
data folder and database (never the repo's ``data/``), creates the admin and
registers the mock as a model endpoint. Recipe from the 2026-09-22 and 09-29
manual runs and scripts/e2e_orchestration.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = "browser-mock"
ADMIN = "admin"
PASSWORD = "browser-test-password-1"

# A reply whose middle stops at the "stream" gate, so a test sees it while it
# is still streaming, and whose end carries raw HTML with an event handler:
# the image reaches the allowed-HTML sanitizer in static/js/markdown.js
# (mutant B13 lets onerror through).
PROBE_STREAM = "(ref BROWSER-STREAM)"
STREAM_FIRST = "FIRST-HALF of the scripted reply. "
STREAM_SECOND = (
    "SECOND-HALF arrives after the gate.\n\n"
    '<img src="/static/missing-probe.png" alt="probe image" onerror="window.__probeXss = 1">\n\n'
    "END-OF-PROBE-REPLY"
)

# Fenced code inside a longer fence. CommonMark closes a fence only on a
# marker at least as long as its opener.
PROBE_FENCE = "(ref BROWSER-FENCE)"
FENCE_REPLY = (
    "Here is the nested example.\n\n"
    "````markdown\n"
    "```python\n"
    "print('inner block')\n"
    "```\n"
    "STILL-INSIDE-OUTER-FENCE\n"
    "````\n\n"
    "END-OF-PROBE-REPLY"
)

# The hand-back: the chat asks the "Browser Scout" loadout to do a task, the
# worker answers, the product hands the result back into the chat, and the
# chat replies quoting it.
PROBE_HANDBACK = "(ref BROWSER-HANDBACK)"
WORKER_MARKER = "(ref BROWSER-WORKER)"
WORKER_LOADOUT = "Browser Scout"
WORKER_TASK = f"Summarise the three fleet notes in one line each and report back. {WORKER_MARKER}"
WORKER_RESULT = "WORKER-RESULT: three notes summarised, nothing blocked."
HANDBACK_RE = re.compile(r"\[Worker [^\]]*\]\s*\nTask:")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _text(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def _chunk(cid: str, delta: dict, finish: str | None = None) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL_ID,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}\n\n"


def _say(text: str) -> list:
    return [text]


def _tool(name: str, args: dict) -> list:
    return [{"tool": name, "args": args}]


class MockModel:
    """Scripted OpenAI-compatible model. ``url`` is the base URL with /v1."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.replies: list[list] = []
        self._gates: dict[str, threading.Event] = {}
        self._reached: dict[str, threading.Event] = {}
        app = FastAPI()

        @app.get("/v1/models")
        async def models():
            return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "tests",
                                                "context_length": 32768}]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            parts = self._script(body)
            self.requests.append(body)
            self.replies.append(parts)
            if body.get("stream"):
                return StreamingResponse(self._stream(parts), media_type="text/event-stream")
            return JSONResponse(self._completion(parts))

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def other(path: str):
            return JSONResponse({"error": {"message": f"mock has no route /{path}"}}, status_code=404)

        import uvicorn

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "MockModel":
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("mock model did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        for gate in self._gates.values():
            gate.set()
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()

    # ── gates ────────────────────────────────────────────────────────────

    def arm(self, name: str) -> None:
        """Make the next reply that passes gate ``name`` wait there."""
        self._gates[name] = threading.Event()
        self._reached[name] = threading.Event()

    def wait_reached(self, name: str, timeout: float = 30) -> None:
        if not self._reached[name].wait(timeout):
            raise AssertionError(f"the mock reply never reached gate {name!r}")

    def release(self, name: str) -> None:
        self._gates[name].set()

    # ── replies ──────────────────────────────────────────────────────────

    def _script(self, body: dict) -> list:
        """The reply as parts: text, ``("gate", name)`` or ``{"tool", "args"}``."""
        messages = body.get("messages") or []
        users = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
        last_user = users[-1] if users else ""
        if body.get("tools"):
            # Checked in this order: the hand-back message quotes the task,
            # and the worker's conversation quotes the person's request
            # (as reference data, before or after its brief).
            if any(HANDBACK_RE.search(u) for u in users) and any(PROBE_HANDBACK in u for u in users):
                return _say("PARENT-FINAL: the Browser Scout reported back. " + WORKER_RESULT)
            if any(WORKER_MARKER in u for u in users):
                return [("gate", "worker"), WORKER_RESULT]
            if any(PROBE_HANDBACK in u for u in users):
                # A later turn may carry the earlier call only as a text record.
                started = "manage_agent_loadout" in json.dumps(
                    [m for m in messages if m.get("role") in ("assistant", "tool")])
                if not started:
                    return _tool("manage_agent_loadout", {"action": "start", "name": WORKER_LOADOUT,
                                                          "task": WORKER_TASK})
                return _say("PARENT-ACK: the Browser Scout is on it; its result comes back here.")
        if PROBE_STREAM in last_user:
            return [STREAM_FIRST, ("gate", "stream"), STREAM_SECOND]
        if PROBE_FENCE in last_user:
            return _say(FENCE_REPLY)
        if not body.get("tools"):
            # Titles, memory extraction and other side requests.
            wants_json = "json" in json.dumps(messages[:1]).lower()
            return _say("[]" if wants_json else "Browser test chat")
        return _say(f"Mock reply to: {last_user.strip()[:200]}")

    async def _stream(self, parts: list):
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        yield _chunk(cid, {"role": "assistant", "content": ""})
        finish = "stop"
        for part in parts:
            if isinstance(part, tuple):  # ("gate", name)
                gate = self._gates.get(part[1])
                if gate is not None:
                    self._reached[part[1]].set()
                    deadline = time.monotonic() + 60
                    while not gate.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                continue
            if isinstance(part, dict):
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                yield _chunk(cid, {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                   "function": {"name": part["tool"], "arguments": ""}}]})
                yield _chunk(cid, {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(part["args"])}}]})
                finish = "tool_calls"
                continue
            for i in range(0, len(part), 24):
                await asyncio.sleep(0.005)
                yield _chunk(cid, {"content": part[i:i + 24]})
        yield _chunk(cid, {}, finish)
        usage = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL_ID,
                 "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        yield f"data: {json.dumps(usage)}\n\n"
        yield "data: [DONE]\n\n"

    @staticmethod
    def _completion(parts: list) -> dict:
        tools = [p for p in parts if isinstance(p, dict)]
        message: dict = {"role": "assistant", "content": "".join(p for p in parts if isinstance(p, str)) or None}
        if tools:
            message["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                                      "function": {"name": t["tool"], "arguments": json.dumps(t["args"])}}
                                     for t in tools]
        return {"id": "mock", "object": "chat.completion", "model": MODEL_ID, "choices": [{
            "index": 0, "finish_reason": "tool_calls" if tools else "stop", "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


class LiveApp:
    """``python app.py`` on a free loopback port with scratch data."""

    def __init__(self, root: Path, model: MockModel) -> None:
        self.root = root
        self.model = model
        self.data = root / "odata"
        self.port = free_port()
        self.log_path = root / "app.stdout.log"
        self.endpoint_id: str | None = None
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("PYTEST") and k not in ("DATABASE_URL", "ODYSSEUS_FILE_LOG")}
        env.update({
            "ODYSSEUS_DATA_DIR": str(self.data),
            # Set explicitly, or a repo .env would point the app at ./data/app.db.
            "DATABASE_URL": f"sqlite:///{(self.data / 'app.db').as_posix()}",
            "APP_BIND": "127.0.0.1", "APP_PORT": str(self.port),
            "AUTH_ENABLED": "true", "LOCALHOST_BYPASS": "false", "SECURE_COOKIES": "false",
            "ODYSSEUS_INPROCESS_POLLERS": "0", "ODYSSEUS_INPROCESS_TASKS": "0",
            # No MCP servers: the built-in browser one runs npx and downloads.
            "ODYSSEUS_DISABLE_MCP": "1",
            # No vector store: retrieval fails fast instead of waiting on one.
            "CHROMADB_HOST": "127.0.0.1", "CHROMADB_PORT": "1",
            "FASTEMBED_CACHE_PATH": str(self.root / "fastembed"),
            "TZ": "UTC", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        })
        return env

    def start(self, timeout: float = 120) -> "LiveApp":
        import httpx

        self.data.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=self._env(),
                                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"app exited with {self.proc.returncode}:\n{self.log_tail()}")
            try:
                if httpx.get(self.url + "/api/auth/status", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"app did not answer within {timeout}s:\n{self.log_tail()}")
            time.sleep(0.2)
        self._provision()
        return self

    @contextlib.contextmanager
    def client(self):
        """``with live_app.client() as client:`` an httpx client signed in as the admin."""
        import httpx

        with httpx.Client(base_url=self.url, timeout=30) as client:
            resp = client.post("/api/auth/login", json={"username": ADMIN, "password": PASSWORD})
            assert resp.status_code == 200 and resp.json().get("ok"), resp.text
            yield client

    def _provision(self) -> None:
        import httpx

        with httpx.Client(base_url=self.url, timeout=30) as setup:
            resp = setup.post("/api/auth/setup", json={"username": ADMIN, "password": PASSWORD})
            assert resp.status_code == 200, resp.text
        with self.client() as client:
            resp = client.post("/api/model-endpoints", data={
                "name": "browser-mock", "base_url": self.model.url, "skip_probe": "true",
                "supports_tools": "true", "pinned_models": json.dumps([MODEL_ID]),
            })
            assert resp.status_code == 200, resp.text
            self.endpoint_id = resp.json()["id"]

    def new_chat(self, name: str, loadout: str | None = None) -> str:
        """A chat on the mock model, optionally under a saved loadout."""
        with self.client() as client:
            resp = client.post("/api/session", data={"name": name, "endpoint_id": self.endpoint_id,
                                                     "model": MODEL_ID})
            assert resp.status_code == 200, resp.text
            sid = resp.json().get("id") or resp.json().get("session_id")
            if loadout:
                resp = client.post(f"/api/agents/sessions/{sid}/loadout", json={"profile": loadout})
                assert resp.status_code == 200, resp.text
        return sid

    def diagnosis(self) -> str:
        """The model requests and the end of the app log, for a failed flow."""
        rows = []
        for body in self.model.requests:
            msgs = body.get("messages") or []
            users = [_text(m.get("content"))[:100].replace("\n", " ") for m in msgs if m.get("role") == "user"]
            roles = "".join((m.get("role") or "?")[0] for m in msgs)
            rows.append(f"tools={bool(body.get('tools'))} roles={roles} first_user={users[:1]} last_user={users[-1:]}")
        rows += [f"reply {i}: {str(parts)[:160]}" for i, parts in enumerate(self.model.replies)]
        return "--- model requests ---\n" + "\n".join(rows) + "\n--- app log ---\n" + self.log_tail(80)

    def log_tail(self, lines: int = 60) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        shutil.rmtree(self.data, ignore_errors=True)
