def _load_module(monkeypatch):
    import routes.editor_draft_routes as mod

    return mod


def test_load_payload_rejects_non_object_json(monkeypatch):
    mod = _load_module(monkeypatch)

    assert mod._load_payload("[]") == {}
    assert mod._load_payload('"draft"') == {}
    assert mod._load_payload("{bad json") == {}
    assert mod._load_payload('{"layers": []}') == {"layers": []}
