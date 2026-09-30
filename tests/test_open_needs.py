"""src/open_needs.py: what a chat is waiting on a person for."""

from src import open_needs


def test_only_user_needs_are_kept_in_order_without_duplicates():
    text = (
        "Outcome: blocked.\n"
        "Needs parent: bash in this worker's workspace\n"
        "Needs user: approve publish request 5ca5dd15\n"
        "- **Needs user:** a GitHub token with repo scope\n"
        "Needs user: approve publish request 5ca5dd15\n"
    )
    assert open_needs.parse(text) == [
        "approve publish request 5ca5dd15",
        "a GitHub token with repo scope",
    ]
    assert open_needs.parse("All done; tests pass.") == []


def test_record_stores_needs_and_a_later_turn_without_any_clears_them(monkeypatch):
    import core.database as db

    store = {}

    def get(sid, **kwargs):
        return dict(store)

    def update(sid, patch):
        for key, value in patch.items():
            if value is None:
                store.pop(key, None)
            else:
                store[key] = value
        return dict(store)

    monkeypatch.setattr(db, "get_session_settings", get)
    monkeypatch.setattr(db, "update_session_settings", update)

    assert open_needs.record("s1", "Blocked.\nNeeds user: pick a base branch") == ["pick a base branch"]
    assert open_needs.read(store) == ["pick a base branch"]
    assert open_needs.record("s1", "Done: rebased on main, tests pass.") == []
    assert open_needs.KEY not in store
    assert open_needs.read({}) == [] and open_needs.read({"open_needs": "junk"}) == []
