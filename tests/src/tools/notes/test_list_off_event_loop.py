"""manage_notes runs on the event loop in the agent loop; the vault walk must not."""
import asyncio
import json
import time

import pytest

from src import tool_implementations
from src.notes_markdown import NoteRecord

SLOW = 0.5


class _SlowStore:
    def __init__(self, notes):
        self.notes = notes

    def list(self, owner=None, *, archived=False, label=None, allow_private=True):
        time.sleep(SLOW)
        return [n for n in self.notes if n.owner == owner and n.archived == archived]

    def find(self, note_id, owner=None, *, allow_private=True):
        time.sleep(SLOW)
        hits = [n for n in self.notes if n.id.startswith(note_id) and n.owner == owner]
        return hits[0] if len(hits) == 1 else None


@pytest.mark.security
async def test_listing_notes_does_not_block_the_loop_and_stays_owner_scoped(monkeypatch):
    from src import notes_store

    store = _SlowStore([
        NoteRecord(id="a1", owner="alice", title="Alice secret plan", content=""),
        NoteRecord(id="b1", owner="bob", title="Bob private plan", content=""),
    ])
    monkeypatch.setattr(notes_store, "STORE", store)

    started = time.monotonic()
    beat_at = []

    async def heartbeat():
        await asyncio.sleep(0.01)
        beat_at.append(time.monotonic() - started)

    beat = asyncio.create_task(heartbeat())
    result = await tool_implementations.do_manage_notes(json.dumps({"action": "list"}), owner="alice")
    await beat

    assert beat_at and beat_at[0] < SLOW / 2, f"loop was blocked for {beat_at} s"
    assert "Alice secret plan" in result["results"]
    assert "Bob" not in result["results"]


@pytest.mark.security
async def test_reading_another_users_note_by_id_still_finds_nothing(monkeypatch):
    from src import notes_store

    monkeypatch.setattr(notes_store, "STORE", _SlowStore(
        [NoteRecord(id="b1-note", owner="bob", title="Bob private plan", content="")]))

    result = await tool_implementations.do_manage_notes(
        json.dumps({"action": "get", "id": "b1"}), owner="alice")

    assert "Bob" not in json.dumps(result)
