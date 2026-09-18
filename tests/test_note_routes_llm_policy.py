from core.middleware import INTERNAL_TOOL_USER
from routes.note import note_routes


class _Store:
    def __init__(self):
        self.calls = []

    def list(self, owner, **kwargs):
        self.calls.append(("list", owner, kwargs))
        return []

    def find(self, note_id, owner, **kwargs):
        self.calls.append(("find", owner, kwargs))
        return None

    def save(self, note, **kwargs):
        self.calls.append(("save", None, kwargs))
        return note

    def delete(self, note_id, owner, **kwargs):
        self.calls.append(("delete", owner, kwargs))
        return True


def test_human_note_routes_bypass_llm_policy(monkeypatch):
    store = _Store()
    monkeypatch.setattr(note_routes, "STORE", store)

    note_routes._list_visible_notes("alice")
    note_routes._find_visible_note("note-1", "alice")
    note_routes._save_authenticated_note(object(), "alice")
    note_routes._delete_authenticated_note("note-1", "alice")

    assert store.calls[0][2]["allow_private"] is True
    assert store.calls[1][2]["allow_private"] is True
    assert store.calls[2][2]["enforce_readonly"] is False
    assert store.calls[3][2]["enforce_readonly"] is False


def test_internal_agent_note_routes_keep_llm_policy(monkeypatch):
    store = _Store()
    monkeypatch.setattr(note_routes, "STORE", store)

    note_routes._list_visible_notes(INTERNAL_TOOL_USER)
    note_routes._find_visible_note("note-1", INTERNAL_TOOL_USER)
    note_routes._save_authenticated_note(object(), INTERNAL_TOOL_USER)
    note_routes._delete_authenticated_note("note-1", INTERNAL_TOOL_USER)

    assert store.calls[0][2]["allow_private"] is False
    assert store.calls[1][2]["allow_private"] is False
    assert store.calls[2][2]["enforce_readonly"] is True
    assert store.calls[3][2]["enforce_readonly"] is True
