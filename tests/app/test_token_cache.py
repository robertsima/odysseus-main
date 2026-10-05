"""app.py's in-memory API-token cache."""


def test_a_refresh_after_an_invalidation_leaves_the_cache_clean(api, monkeypatch):
    # Creating, revoking or renaming a token calls invalidate_token_cache().
    # The bearer-token middleware refreshes the cache while it reads dirty, so
    # a flag that stays set reloads every token from the database on every
    # API request.
    state = api.app.state
    monkeypatch.setattr(api.module, "_token_cache", {})
    monkeypatch.setattr(state, "_token_cache", api.module._token_cache)
    monkeypatch.setattr(state, "_token_cache_dirty", False)

    state.invalidate_token_cache()
    assert state._token_cache_dirty is True

    api.module._refresh_token_cache()

    assert state._token_cache_dirty is False
