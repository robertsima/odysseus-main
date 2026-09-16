import asyncio
import json
from src import tool_implementations
from src.notes_markdown import NoteItem, NoteRecord
from tests.helpers.fake_notes_store import FakeNotesStore


def _install_fakes(monkeypatch, note):
    from src import notes_store
    store = FakeNotesStore(note)
    monkeypatch.setattr(notes_store, "STORE", store)
    return store


def _run(args, owner="alice"):
    return asyncio.run(tool_implementations.do_manage_notes(json.dumps(args), owner=owner))


def _note(owner=None, **overrides):
    data = {
        "id": "abc12345-existing", "owner": owner, "title": "Original",
        "content": "", "color": None, "label": None,
        "items": [NoteItem(text="item", done=False)], "pinned": False,
        "archived": False, "due_date": None,
    }
    data.update(overrides)
    return NoteRecord(**data)


def test_update_rejects_legacy_null_owner_for_authenticated_owner(monkeypatch):
    note = _note(owner=None)
    store = _install_fakes(monkeypatch, note)

    result = _run({"action": "update", "id": "abc12345", "title": "Changed"})

    assert result == {"error": "Note not found", "exit_code": 1}
    assert note.title == "Original"
    assert store.saved == 0


def test_delete_rejects_legacy_empty_owner_for_authenticated_owner(monkeypatch):
    note = _note(owner="")
    store = _install_fakes(monkeypatch, note)

    result = _run({"action": "delete", "id": "abc12345"})

    assert result == {"error": "Note not found", "exit_code": 1}
    assert store.deleted == []
    assert store.saved == 0


def test_toggle_rejects_other_owner(monkeypatch):
    note = _note(owner="bob")
    store = _install_fakes(monkeypatch, note)

    result = _run({"action": "toggle_item", "id": "abc12345", "index": 0})

    assert result == {"error": "Note not found", "exit_code": 1}
    assert note.items[0].done is False
    assert store.saved == 0


def test_update_allows_matching_owner(monkeypatch):
    note = _note(owner="alice")
    store = _install_fakes(monkeypatch, note)

    result = _run({"action": "update", "id": "abc12345", "title": "Changed"})

    assert result["exit_code"] == 0
    assert note.title == "Changed"
    assert store.saved == 1
