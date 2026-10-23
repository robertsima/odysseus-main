"""The note-due scanner must not read the vault on the event loop.

Production, 2026-10-23, app.log [loop-lag]: 1.88 s in ``Path.read_text`` and
2.46 s in ``realpath``, both <- ``MarkdownNotesStore.list`` <-
``action_ping_notes`` <- task_scheduler. The vault sits on a slow NAS, so every
chat stream froze while the scanner walked it.
"""
import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.notes_markdown import NoteRecord

SLOW = 0.5


@pytest.fixture
def vault(monkeypatch, tmp_path):
    import src.notes_store as module
    import src.builtin_actions as actions

    values = {"notes_directory": "Notes", "notes_archive_directory": "Notes/Archive"}
    monkeypatch.setattr(module, "vault_root", lambda: str(tmp_path / "vault"))
    monkeypatch.setattr(module, "get_setting", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path / "data"))
    return module.STORE


@pytest.mark.security
async def test_ping_notes_keeps_the_loop_responsive_and_pings_only_the_owners_notes(vault, monkeypatch):
    import routes.note_routes as note_routes
    from src.builtin_actions import action_ping_notes

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    vault.save(NoteRecord(id="n-alice", owner="alice", title="Alice due", content="", due_date=now))
    vault.save(NoteRecord(id="n-bob", owner="bob", title="Bob due", content="", due_date=now))
    pinged = []

    async def dispatch(*, title, note_body, note_id, owner):
        pinged.append((note_id, owner))

    monkeypatch.setattr(note_routes, "dispatch_reminder", dispatch)

    real = vault.list

    def slow(*args, **kwargs):
        time.sleep(SLOW)  # a NAS read: blocks whichever thread calls it
        return real(*args, **kwargs)

    monkeypatch.setattr(vault, "list", slow)

    started = time.monotonic()
    beat_at = []

    async def heartbeat():
        await asyncio.sleep(0.01)
        beat_at.append(time.monotonic() - started)

    beat = asyncio.create_task(heartbeat())
    message, ok = await action_ping_notes(owner="alice")
    await beat

    assert beat_at and beat_at[0] < SLOW / 2, f"loop was blocked for {beat_at} s"
    assert ok and "Pinged 1 note(s): Alice due" in message
    assert pinged == [("n-alice", "alice")]
