"""One set of write rules, and a schema that describes only settings that work.

The 2026-09-28 configuration review found the operator facing settings that did
nothing (an advisory round budget nobody enforced, a remote-host inventory no
tool read, a slow-request threshold only the environment could set), settings
the UI wrote but the backend dropped (the reminder email account, the worker
nesting depth), and two write routes that disagreed about what a valid value
is: the Settings tabs' route never ran the schema's validators, and the
Configuration panel's route never ran the tabs' per-key rules, so a gpt-* model
saved from the panel broke every Claude Code delegation.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.auth_routes as auth_routes
import src.settings as settings_mod
from src import settings_schema


# ── helpers ─────────────────────────────────────────────────────────────


class _AuthManager:
    def get_username_for_token(self, token):
        return "admin" if token == "admin-session" else None

    def is_admin(self, username):
        return username == "admin"


class _AuthRequest(SimpleNamespace):
    def __init__(self, body):
        super().__init__(cookies={auth_routes.SESSION_COOKIE: "admin-session"}, _body=body)

    async def json(self):
        return self._body


class _SchemaRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _endpoint(router, path, method):
    return next(
        route.endpoint for route in router.routes
        if route.path == path and method in route.methods
    )


@pytest.fixture
def auth_store(monkeypatch):
    """POST /api/auth/settings against an in-memory settings file."""
    store = dict(settings_mod.DEFAULT_SETTINGS)
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(store))

    def save(updated):
        store.clear()
        store.update(updated)

    monkeypatch.setattr(auth_routes, "_save_settings", save)
    post = _endpoint(auth_routes.setup_auth_routes(_AuthManager()), "/api/auth/settings", "POST")

    def call(body):
        return asyncio.run(post(_AuthRequest(body)))

    return store, call


@pytest.fixture
def schema_store(monkeypatch):
    """POST /api/settings/schema against an in-memory settings file."""
    import routes.capability_routes as capability_routes
    import src.tool_security as sec

    store = dict(settings_mod.DEFAULT_SETTINGS)
    monkeypatch.setattr(capability_routes, "require_user", lambda _r: "admin")
    monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: True)
    monkeypatch.setattr("src.settings.load_settings", lambda: dict(store))

    def save(updated):
        store.clear()
        store.update(updated)

    monkeypatch.setattr("src.settings.save_settings", save)
    post = _endpoint(capability_routes.setup_capability_routes(), "/api/settings/schema", "POST")

    def call(settings):
        return asyncio.run(post(_SchemaRequest({"settings": settings})))

    return store, call


def _payload_keys(**kwargs):
    return {
        item["key"]: item
        for group in settings_schema.ui_payload(**kwargs)
        for item in group["settings"]
    }


# ── retired and removed settings ────────────────────────────────────────


def test_a_retired_key_is_not_declared_so_the_panel_cannot_save_it(schema_store):
    """default_model_fallbacks was auto-declared, so the panel rendered it and
    POST /api/settings/schema stored it, past the tombstone."""
    assert "default_model_fallbacks" in settings_mod.RETIRED_SETTING_KEYS
    assert settings_schema.get_spec("default_model_fallbacks") is None
    assert settings_schema.missing_specs() == []
    assert "default_model_fallbacks" not in _payload_keys(is_admin=True)

    _store, call = schema_store
    with pytest.raises(HTTPException) as exc:
        call({"default_model_fallbacks": "[]"})
    assert exc.value.status_code == 400
    assert "Unknown setting" in str(exc.value.detail)


@pytest.mark.parametrize("key", ["agent_max_rounds", "slow_request_log_seconds", "remote_hosts"])
def test_settings_that_did_nothing_are_gone(key):
    assert key not in settings_mod.DEFAULT_SETTINGS
    assert settings_schema.get_spec(key) is None
    assert key not in settings_schema._MIGRATED_ENV_OVERRIDES


def test_the_remote_hosts_capability_is_gone():
    import src.capabilities_builtin  # noqa: F401
    from src import capabilities

    assert capabilities.get("remote_hosts") is None
    assert not any(
        group["group"] == "Remote hosts" for group in settings_schema.ui_payload(is_admin=True)
    )


def test_a_stored_round_budget_is_ignored_by_the_settings_route(auth_store):
    store, call = auth_store
    call({"agent_max_rounds": 7, "agent_max_tool_calls": 40})
    assert "agent_max_rounds" not in store
    assert store["agent_max_tool_calls"] == 40


# ── hidden specs ────────────────────────────────────────────────────────

_HIDDEN = (
    "tts_enabled", "tts_provider", "tts_model", "tts_voice", "tts_speed", "tts_cache_max_bytes",
    "teacher_enabled", "teacher_model", "teacher_tier2_enabled",
)


def test_dormant_features_are_declared_but_not_rendered():
    rendered = _payload_keys(is_admin=True)
    for key in _HIDDEN:
        spec = settings_schema.get_spec(key)
        assert spec is not None and spec.hidden, key
        assert key in settings_mod.DEFAULT_SETTINGS, f"{key}: the backend still reads it"
        assert key not in rendered, key
    assert "chat_upload_max_bytes" in rendered, "only the hidden specs are left out"


def test_the_schema_endpoint_omits_hidden_specs(monkeypatch):
    import routes.capability_routes as capability_routes
    import src.tool_security as sec

    monkeypatch.setattr(capability_routes, "require_user", lambda _r: "admin")
    monkeypatch.setattr(sec, "owner_is_admin_or_single_user", lambda _o: True)
    get = _endpoint(capability_routes.setup_capability_routes(), "/api/settings/schema", "GET")
    out = asyncio.run(get(_SchemaRequest({})))
    served = {item["key"] for group in out["groups"] for item in group["settings"]}
    assert served and not served & set(_HIDDEN)


def test_a_hidden_setting_is_still_saved_and_validated(auth_store):
    store, call = auth_store
    call({"tts_speed": "0.5", "tts_enabled": False})
    assert store["tts_speed"] == "0.5", "the Voice tab offers 0.5x"
    assert store["tts_enabled"] is False
    with pytest.raises(HTTPException):
        call({"tts_speed": "9"})


# ── keys the UI writes ──────────────────────────────────────────────────


def test_reminder_account_and_worker_depth_are_real_settings():
    from src.headless_agent import DEFAULT_MAX_WORKER_DEPTH

    assert settings_mod.DEFAULT_SETTINGS["reminder_email_account_id"] == ""
    assert settings_schema.get_spec("reminder_email_account_id").group == "Reminders"

    assert settings_mod.DEFAULT_SETTINGS["agent_max_worker_depth"] == DEFAULT_MAX_WORKER_DEPTH
    spec = settings_schema.get_spec("agent_max_worker_depth")
    assert spec.group == "Agents" and spec.advanced
    # headless_agent.max_worker_depth clamps to the same range.
    assert (spec.min_value, spec.max_value) == (1, 4)


def test_the_settings_route_keeps_what_the_reminders_tab_writes(auth_store):
    """Dropped until 2026-09-28: the key was not in DEFAULT_SETTINGS."""
    store, call = auth_store
    call({"reminder_email_account_id": "acct-2", "agent_max_worker_depth": 9})
    assert store["reminder_email_account_id"] == "acct-2"
    assert store["agent_max_worker_depth"] == 4, "clamped to the declared range"
    # The tab sends null for "default account".
    call({"reminder_email_account_id": None})
    assert store["reminder_email_account_id"] == ""


# ── one set of write rules ──────────────────────────────────────────────


@pytest.mark.parametrize("clamp", [True, False])
def test_every_shipped_default_passes_the_write_rules_unchanged(clamp):
    for key, value in settings_mod.DEFAULT_SETTINGS.items():
        if key in settings_mod.RETIRED_SETTING_KEYS:
            continue
        assert settings_schema.normalize_value(key, value, clamp=clamp) == value, key


def test_the_settings_route_now_runs_the_schema_validators(auth_store):
    """A malformed folder policy reads as "everything private" (fail-closed);
    refusing it on write names the bad entry instead."""
    store, call = auth_store
    with pytest.raises(HTTPException) as exc:
        call({"vault_folder_sensitivity": {"Journal": "secret"}, "notes_directory": "Other"})
    assert exc.value.status_code == 400
    assert "vault_folder_sensitivity" in str(exc.value.detail)
    assert store["notes_directory"] == "Notes", "nothing is written when one key is refused"


def test_the_settings_route_now_checks_choice_lists(auth_store):
    _store, call = auth_store
    with pytest.raises(HTTPException) as exc:
        call({"image_quality": "ultra"})
    assert "image_quality" in str(exc.value.detail)


def test_the_settings_route_clamps_to_the_schema_ranges(auth_store):
    store, call = auth_store
    call({"agent_input_token_hard_max": 5_000_000, "agent_max_tool_calls": "12"})
    assert store["agent_input_token_hard_max"] == 1_000_000
    assert store["agent_max_tool_calls"] == 12
    with pytest.raises(HTTPException):
        call({"agent_max_tool_calls": "lots"})


def test_the_context_cap_ceiling_matches_the_agents_tab():
    spec = settings_schema.get_spec("agent_input_token_hard_max")
    assert spec.max_value == 1_000_000
    with pytest.raises(ValueError, match="no more than"):
        settings_schema.normalize_value("agent_input_token_hard_max", 2_000_000)


def test_the_panel_route_applies_the_claude_rules(schema_store):
    store, call = schema_store
    out = call({"claude_code_model": "Opus 5.5"})
    assert out["saved"] == ["claude_code_model"]
    assert store["claude_code_model"] == "claude-opus-5-5"

    with pytest.raises(HTTPException) as exc:
        call({"claude_code_model": "gpt-5.4"})
    assert "not a Claude model" in str(exc.value.detail)
    assert store["claude_code_model"] == "claude-opus-5-5"

    call({"claude_cloud_repositories": "https://github.com/acme/app.git, *"})
    assert store["claude_cloud_repositories"] == ["acme/app", "*"]
    with pytest.raises(HTTPException):
        call({"claude_cloud_repositories": "not a slug/at all/x"})
    with pytest.raises(HTTPException):
        call({"claude_code_binary": "relative/claude"})


def test_the_settings_route_applies_the_same_claude_rules(auth_store):
    store, call = auth_store
    call({"claude_code_model": "default", "claude_code_backend": "CLOUD",
          "claude_cloud_workflow": ""})
    assert store["claude_code_model"] == ""
    assert store["claude_code_backend"] == "cloud"
    assert store["claude_cloud_workflow"] == "odysseus-claude.yml"
    with pytest.raises(HTTPException):
        call({"chatgpt_reasoning_effort": "maximum"})


def test_the_claude_code_model_control_offers_claude_aliases_not_endpoint_models():
    from src.agent_tools.claude_code_tools import _CLAUDE_MODEL_ALIASES

    spec = settings_schema.get_spec("claude_code_model")
    assert spec.options_source == "", "endpoint models (gpt-*) are not Claude Code models"
    assert set(spec.suggestions) == set(_CLAUDE_MODEL_ALIASES) - {"default"}


# ── per-user flags ──────────────────────────────────────────────────────


def test_per_user_flags_follow_the_resolver():
    flagged = {spec.key for spec in settings_schema.all_specs() if spec.per_user}
    assert flagged == set(settings_mod._PER_USER_KEYS)


def test_the_panel_shows_what_it_saves_and_the_viewers_own_override(monkeypatch):
    """The panel saves the global value, as the Settings tabs do; showing the
    admin's personal override as the value made a save look ignored."""
    import routes.prefs_routes as prefs_routes

    saved = dict(settings_mod.DEFAULT_SETTINGS, default_model="global-model", stt_model="small")
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: saved.get(key, default))
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: {"default_model": "my-model"})

    rendered = _payload_keys(owner="admin", is_admin=True)
    assert rendered["default_model"]["value"] == "global-model"
    assert rendered["default_model"]["user_value"] == "my-model"
    assert rendered["stt_model"]["user_value"] is None
    assert "user_value" not in rendered["search_provider"], "not a per-user key"


# ── search provider names ───────────────────────────────────────────────


def test_research_provider_google_is_stored_as_google_pse(schema_store, auth_store):
    spec = settings_schema.get_spec("research_search_provider")
    assert "google_pse" in spec.choices and "google" not in spec.choices

    panel_store, panel_call = schema_store
    panel_call({"research_search_provider": "google"})
    assert panel_store["research_search_provider"] == "google_pse"

    tab_store, tab_call = auth_store
    tab_call({"research_search_provider": "google"})
    assert tab_store["research_search_provider"] == "google_pse"


def test_search_fallback_none_is_a_chain_of_its_own():
    assert settings_mod.DEFAULT_SETTINGS["search_fallback_chain"] == []
    assert settings_schema.normalize_value("search_fallback_chain", ["none", "brave"]) == ["none"]
    assert settings_schema.normalize_value("search_fallback_chain", "brave, google") == ["brave", "google_pse"]
    assert "none" in settings_schema.get_spec("search_fallback_chain").help


def test_model_fallback_lists_are_edited_as_json_not_as_lines():
    """A one-name-per-line control would save [{…}] back as "[object Object]"."""
    for key in ("utility_model_fallbacks", "vision_model_fallbacks"):
        assert settings_schema.get_spec(key).type == "json", key
        settings_schema.normalize_value(key, [{"endpoint_id": "e1", "model": "m"}])
        with pytest.raises(ValueError):
            settings_schema.normalize_value(key, ["[object Object]"])


def test_research_limits_sit_together_under_research_advanced():
    """The planning and query timeouts have no control in the Research tab;
    they belong next to the other research limits, with the reader's ranges."""
    for key, low, high in (
        ("research_planning_timeout_seconds", 15, 3600),
        ("research_query_timeout_seconds", 15, 3600),
        ("research_extraction_timeout_seconds", 15, 3600),
        ("research_extraction_concurrency", 1, 12),
        ("research_run_timeout_seconds", 0, 86400),
    ):
        spec = settings_schema.get_spec(key)
        assert (spec.group, spec.advanced, spec.min_value, spec.max_value) == ("Research", True, low, high), key
        assert spec.unit and spec.label != key.replace("_", " ").capitalize(), key
