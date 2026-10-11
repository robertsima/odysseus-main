import pytest


@pytest.fixture
def outcomes(monkeypatch, app_db):
    """src.outcomes bound to a throwaway database."""
    import core.database as cdb
    from src import outcomes as store

    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    return store
