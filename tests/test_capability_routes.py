"""The admin surface for core configuration.

Core configuration is admin-only by design: capability state names host paths and
missing binaries, and the settings schema spans every user's defaults. The
project is moving toward genuine multi-user deployments, so these are treated as
a privilege boundary rather than a UI convenience — a non-admin who crafts the
request by hand must be refused, not merely shown a page without the controls.

The validation tests exist because the failure they prevent is silent: a bool
stored as the string "false" reads as truthy, so a capability the operator
switched off would come back on.
"""

import pytest
from fastapi import HTTPException


def _router():
    from routes.capability_routes import setup_capability_routes

    return setup_capability_routes()


def _handler(path: str, method: str):
    for route in _router().routes:
        if route.path == path and method.upper() in route.methods:
            return route.endpoint
    raise AssertionError(f"no handler for {method} {path}")


class _Req:
    """Minimal Request stand-in: these handlers only read the JSON body."""

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


class TestRoutesExist:
    def test_the_expected_surface_is_registered(self):
        paths = {r.path for r in _router().routes}
        assert paths == {
            "/api/capabilities",
            "/api/capabilities/{name}",
            "/api/capabilities/recheck",
            "/api/settings/schema",
        }


class TestAdminBoundary:
    @pytest.mark.parametrize("path,method", [
        ("/api/capabilities", "GET"),
        ("/api/capabilities/{name}", "POST"),
        ("/api/capabilities/recheck", "POST"),
        ("/api/settings/schema", "POST"),
    ])
    def test_non_admin_is_refused(self, path, method, monkeypatch):
        """Refused at the handler, not merely hidden in the UI."""
        import routes.capability_routes as mod
        import src.tool_security as sec

        monkeypatch.setattr(mod, "require_user", lambda _r: "bob")
        monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: False)

        handler = _handler(path, method)
        with pytest.raises(HTTPException) as exc:
            import asyncio

            kwargs = {"name": "vault"} if "{name}" in path else {}
            asyncio.run(handler(_Req({"enabled": True}), **kwargs))
        assert exc.value.status_code == 403

    def test_schema_read_is_allowed_for_a_non_admin(self, monkeypatch):
        """A user still needs their own settings page; it just carries fewer
        controls, so the read is not admin-gated."""
        import asyncio

        import routes.capability_routes as mod
        import src.tool_security as sec

        monkeypatch.setattr(mod, "require_user", lambda _r: "bob")
        monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: False)

        out = asyncio.run(_handler("/api/settings/schema", "GET")(_Req()))
        assert out["is_admin"] is False
        assert isinstance(out["groups"], list)


class TestSettingsValidation:
    def test_unknown_keys_are_rejected_not_stored(self, monkeypatch):
        """A typo that persists is a setting that looks saved and does nothing."""
        import asyncio

        import routes.capability_routes as mod
        import src.tool_security as sec

        monkeypatch.setattr(mod, "require_user", lambda _r: "admin")
        monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: True)

        saved = {}
        monkeypatch.setattr("src.settings.save_settings", lambda d: saved.update(d))

        with pytest.raises(HTTPException) as exc:
            asyncio.run(_handler("/api/settings/schema", "POST")(
                _Req({"settings": {"notes_directory": "Notes", "nope_not_real": 1}})
            ))
        assert exc.value.status_code == 400
        assert "nope_not_real" in str(exc.value.detail)
        assert saved == {}, "nothing may be written when any key is invalid"

    def test_env_pinned_settings_are_reported_not_silently_accepted(self, monkeypatch):
        """Accepting a pinned value would tell the operator a change took effect
        when the deployment overrides it."""
        import asyncio

        import routes.capability_routes as mod
        import src.tool_security as sec

        monkeypatch.setattr(mod, "require_user", lambda _r: "admin")
        monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: True)
        monkeypatch.setenv("ODYSSEUS_PERSONAL_DIR", "/pinned/by/deployment")
        monkeypatch.setattr("src.settings.save_settings", lambda _d: None)

        out = asyncio.run(_handler("/api/settings/schema", "POST")(
            _Req({"settings": {"vault_directory": "/ignored"}})
        ))
        assert out["locked_by_environment"] == ["vault_directory"]
        assert out["saved"] == []

    @pytest.mark.parametrize("raw,expected", [
        ("false", False), ("FALSE", False), ("0", False), ("off", False), ("no", False),
        ("true", True), ("1", True), ("on", True), (True, True), (False, False),
    ])
    def test_booleans_coerce_by_meaning_not_truthiness(self, raw, expected):
        """`bool("false")` is True. Getting this wrong turns a capability the
        operator disabled back on."""
        from routes.capability_routes import _coerce
        from src import settings_schema

        spec = settings_schema.get_spec("agent_peer_messaging")
        assert spec is not None
        assert _coerce(spec, raw) is expected

    def test_choice_values_outside_the_declared_set_are_refused(self):
        from routes.capability_routes import _coerce
        from src import settings_schema

        spec = settings_schema.get_spec("vault_default_sensitivity")
        assert _coerce(spec, "private") == "private"
        with pytest.raises(HTTPException) as exc:
            _coerce(spec, "sort-of-private")
        assert exc.value.status_code == 400

    def test_malformed_json_is_refused_with_a_usable_message(self):
        from routes.capability_routes import _coerce
        from src import settings_schema

        spec = settings_schema.get_spec("vault_folder_sensitivity")
        with pytest.raises(HTTPException) as exc:
            _coerce(spec, "{not json")
        assert exc.value.status_code == 400
        assert "vault_folder_sensitivity" in str(exc.value.detail)

    def test_int_rejects_non_numeric(self):
        from routes.capability_routes import _coerce
        from src import settings_schema

        spec = settings_schema.get_spec("agent_peer_message_budget")
        assert _coerce(spec, "12") == 12
        with pytest.raises(HTTPException):
            _coerce(spec, "lots")


class TestCapabilityToggle:
    def test_unknown_capability_is_a_404(self, monkeypatch):
        import asyncio

        import routes.capability_routes as mod
        import src.tool_security as sec

        monkeypatch.setattr(mod, "require_user", lambda _r: "admin")
        monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: True)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(_handler("/api/capabilities/{name}", "POST")(
                _Req({"enabled": True}), name="not_a_capability"
            ))
        assert exc.value.status_code == 404

    def test_enabling_an_unsatisfied_capability_is_allowed_and_honest(self, monkeypatch):
        """An operator may switch something on before installing its dependency.
        The status then names what is missing, and the tool stays out of the
        schema until it is met — so nothing is advertised early."""
        import asyncio

        import routes.capability_routes as mod
        import src.tool_security as sec
        from src import capabilities

        monkeypatch.setattr(mod, "require_user", lambda _r: "admin")
        monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: True)

        capabilities.register(capabilities.Capability(
            name="_t_route_cap", title="Route cap", summary="", feature_key="_t_route_cap",
            requirements=(capabilities.Requirement(
                name="absent thing", check=lambda: (False, "absent"), hint="install it"),),
            tools=("_t_route_tool",),
        ))
        features = {}
        monkeypatch.setattr("src.settings.load_features", lambda: dict(features))
        monkeypatch.setattr("src.settings.save_features", lambda d: features.update(d))

        out = asyncio.run(_handler("/api/capabilities/{name}", "POST")(
            _Req({"enabled": True}), name="_t_route_cap"
        ))
        cap = out["capability"]
        assert cap["enabled"] is True
        assert cap["satisfied"] is False
        assert cap["available"] is False
        assert cap["unmet"][0]["hint"] == "install it"
        assert "_t_route_tool" in capabilities.unavailable_tools()
