import pytest

from tests.src.capture.conftest import FakeTd


@pytest.fixture
def td(monkeypatch):
    from src import capture

    fake = FakeTd()
    monkeypatch.setenv("TODOIST_API_TOKEN", "test-token")
    monkeypatch.setattr(capture, "_run_td", fake)
    return fake


@pytest.fixture
def notes(monkeypatch):
    from src import notes_store
    from tests.helpers.fake_notes_store import FakeNotesStore

    store = FakeNotesStore()
    monkeypatch.setattr(notes_store, "STORE", store)
    return store
