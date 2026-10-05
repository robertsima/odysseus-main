"""The MCP memory list returns every entry, not a first page."""
import asyncio

import mcp_servers.memory_server as memory_server
from src.memory import MemoryManager


def test_list_returns_every_memory(monkeypatch, tmp_path):
    manager = MemoryManager(str(tmp_path))
    manager.save([manager.add_entry(f"fact number {i}", category="fact") for i in range(130)])
    monkeypatch.setattr(memory_server, "_memory_manager", manager)
    monkeypatch.setattr(memory_server, "_memory_vector", None)
    monkeypatch.setattr(memory_server, "_initialized", True)
    for key in memory_server._OWNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    result = asyncio.run(memory_server.call_tool("manage_memory", {"action": "list"}))

    text = result[0].text
    assert "Found 130 memory entries" in text
    assert "fact number 129" in text
