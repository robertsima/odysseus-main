"""The native memory list returns every entry, not a first page."""
import asyncio

from src import ai_interaction
from src.memory import MemoryManager


def test_list_returns_every_memory(monkeypatch, tmp_path):
    manager = MemoryManager(str(tmp_path))
    manager.save([manager.add_entry(f"fact number {i}", category="fact") for i in range(130)])
    monkeypatch.setattr(ai_interaction, "_memory_manager", manager)

    result = asyncio.run(ai_interaction.do_manage_memory("list"))

    assert "Found 130 memory entries" in result["results"]
    assert "fact number 129" in result["results"]
