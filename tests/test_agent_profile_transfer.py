"""Exporting and importing agent profiles (loadouts).

An export is a portable, versioned document holding only what the validator
keeps, with credentials pasted into free text redacted. An import is not a side
door: every profile is validated again, the agent tool clamps each to the
calling chat's policy exactly as ``create`` does, and the admin routes use the
same gate as saving profiles through Settings.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import agent_loadouts, agent_profile_transfer, agent_profiles
from src.agent_tools import loadout_tools
from src.agent_tools.loadout_tools import manage_agent_loadout

ROOT = Path(__file__).resolve().parent.parent


def _profile(name, **fields):
    return agent_profiles.validate_profiles([{"name": name, **fields}])[0]


@pytest.fixture
def store(monkeypatch):
    saved = {"profiles": [], "writes": 0}

    def write(profiles):
        saved["profiles"] = agent_profiles.validate_profiles(list(profiles))
        saved["writes"] += 1

    monkeypatch.setattr(agent_loadouts, "_write", write)
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    return saved


def _doc(*profiles, **overrides):
    return {"format": "odysseus-agent-profiles", "version": 1, "exported_at": "2026-09-23T00:00:00Z",
            "profiles": list(profiles), **overrides}


# ── export ───────────────────────────────────────────────────────────────────

def test_export_is_a_versioned_document_of_validated_profiles(store):
    store["profiles"] = [_profile("Reviewer", description="reads diffs", tool_access="selected",
                                  enabled_tools=["grep", "read_file"]),
                         _profile("Writer")]
    doc = agent_profile_transfer.export_profiles()
    assert doc["format"] == "odysseus-agent-profiles"
    assert doc["version"] == 1
    assert doc["exported_at"].endswith("Z")
    assert [p["name"] for p in doc["profiles"]] == ["Reviewer", "Writer"]
    assert set(doc["profiles"][0]) == set(_profile("X"))
    json.dumps(doc)  # serialisable as-is


def test_export_can_pick_profiles_by_name_and_refuses_unknown_names(store):
    store["profiles"] = [_profile("Reviewer"), _profile("Writer")]
    assert [p["name"] for p in agent_profile_transfer.export_profiles("writer")["profiles"]] == ["Writer"]
    assert [p["name"] for p in agent_profile_transfer.export_profiles(["Writer", "Reviewer"])["profiles"]] == [
        "Writer", "Reviewer"]
    with pytest.raises(ValueError, match="Ghost"):
        agent_profile_transfer.export_profiles("Writer,Ghost")


def test_secrets_are_not_exported(store):
    stored = _profile("Deployer", instructions=(
        "Use api_key=sk-live-abcdefghijklmnopqrstuvwx and Authorization: Bearer abcdef123456789\n"
        "Fetch https://user:hunter22@example.com/path?token=zzz and ghp_abcdefghijklmnopqrstuvwxyz123456"),
        description="token: xoxb-1234567890-abcdef", tool_access="selected", enabled_tools=["grep"])
    store["profiles"] = [{**stored, "api_key": "sk-should-never-leave", "owner": "alice"}]
    text = json.dumps(agent_profile_transfer.export_profiles())
    for secret in ("sk-live-abcdefghijklmnopqrstuvwx", "abcdef123456789", "hunter22", "token=zzz",
                   "ghp_abcdefghijklmnopqrstuvwxyz123456", "xoxb-1234567890", "sk-should-never-leave", "alice"):
        assert secret not in text
    assert "https://example.com/path" in text  # the useful part of the text survives


def test_export_drops_the_machine_tool_inventory_snapshot(store):
    store["profiles"] = [_profile("Narrow", tool_access="selected", enabled_tools=["grep"],
                                  disabled_tools=["bash", "mcp__localbox__shell", "write_file"]),
                         _profile("Open", disabled_tools=["bash"])]
    by_name = {p["name"]: p for p in agent_profile_transfer.export_profiles()["profiles"]}
    assert by_name["Narrow"]["disabled_tools"] == []
    assert by_name["Open"]["disabled_tools"] == ["bash"]  # an explicit denial under "all" still means something


# ── import ───────────────────────────────────────────────────────────────────

def test_round_trip_export_then_import_restores_the_profiles(store):
    original = [_profile("Reviewer", description="reads diffs", tool_access="selected",
                         enabled_tools=["grep", "read_file"], temperature=0.3, max_rounds=20),
                _profile("Writer", memory_access="write", delegation_policy="never")]
    store["profiles"] = list(original)
    doc = json.loads(json.dumps(agent_profile_transfer.export_profiles()))
    store["profiles"] = []
    report = agent_profile_transfer.import_profiles(doc)
    assert report["added"] == ["Reviewer", "Writer"]
    assert report["errors"] == [] and report["written"] is True
    assert store["profiles"] == original


def test_merge_overwrites_same_names_and_keeps_the_rest(store):
    store["profiles"] = [_profile("Reviewer", description="old"), _profile("Keeper")]
    report = agent_profile_transfer.import_profiles(
        _doc({"name": "reviewer", "description": "new"}, {"name": "Fresh"}), mode="merge")
    assert report["updated"] == ["reviewer"] and report["added"] == ["Fresh"]
    assert [p["name"] for p in store["profiles"]] == ["reviewer", "Keeper", "Fresh"]
    assert store["profiles"][0]["description"] == "new"


def test_merge_can_keep_both_by_renaming(store):
    store["profiles"] = [_profile("Reviewer", description="old")]
    report = agent_profile_transfer.import_profiles(
        _doc({"name": "Reviewer", "description": "new"}), rename_conflicts=True)
    assert report["added"] == ["Reviewer 2"] and report["updated"] == []
    assert [(p["name"], p["description"]) for p in store["profiles"]] == [("Reviewer", "old"), ("Reviewer 2", "new")]


def test_replace_makes_the_file_the_whole_list(store):
    store["profiles"] = [_profile("Reviewer"), _profile("Gone")]
    report = agent_profile_transfer.import_profiles(_doc({"name": "Reviewer"}, {"name": "New"}), mode="replace")
    assert report["updated"] == ["Reviewer"] and report["added"] == ["New"] and report["removed"] == ["Gone"]
    assert [p["name"] for p in store["profiles"]] == ["Reviewer", "New"]


def test_replace_with_any_invalid_profile_writes_nothing(store):
    store["profiles"] = [_profile("Keeper")]
    report = agent_profile_transfer.import_profiles(
        _doc({"name": "Fine"}, {"name": "Bad", "memory_access": "everything"}), mode="replace")
    assert report["written"] is False and store["writes"] == 0
    assert [p["name"] for p in store["profiles"]] == ["Keeper"]
    assert report["errors"][0]["name"] == "Bad"
    assert report["skipped"] == [{"name": "Fine", "reason": "replace writes nothing while the file has errors"}]


def test_validation_errors_are_reported_per_profile_and_the_rest_merge(store):
    report = agent_profile_transfer.import_profiles(_doc(
        {"name": "Good"},
        {"name": "!!bad name"},
        {"name": "Loose", "approval_mode": "yolo"},
        "not an object",
        {"name": "good"},
    ))
    assert report["added"] == ["Good"]
    errors = {e["index"]: e["error"] for e in report["errors"]}
    assert set(errors) == {1, 2, 3, 4}
    assert "name must be" in errors[1]
    assert "approval_mode" in errors[2]
    assert "must be an object" in errors[3]
    assert "duplicate" in errors[4]


@pytest.mark.parametrize("doc,match", [
    ([], "JSON object"),
    ({"format": "something-else", "version": 1, "profiles": []}, "format"),
    ({"profiles": []}, "format"),
    (_doc(version=2), "version"),
    (_doc(version="1"), "version"),
    (_doc(version=True), "version"),
    (_doc(profiles={"name": "x"}), "list"),
])
def test_an_unknown_format_or_version_is_rejected_whole(store, doc, match):
    with pytest.raises(ValueError, match=match):
        agent_profile_transfer.import_profiles(doc)
    assert store["writes"] == 0


def test_the_profile_count_is_capped(store):
    too_many = [{"name": f"P{i}"} for i in range(agent_profiles.MAX_PROFILES + 1)]
    with pytest.raises(ValueError, match=str(agent_profiles.MAX_PROFILES)):
        agent_profile_transfer.import_profiles(_doc(*too_many))
    store["profiles"] = [_profile(f"Old{i}") for i in range(agent_profiles.MAX_PROFILES - 1)]
    report = agent_profile_transfer.import_profiles(_doc({"name": "A"}, {"name": "B"}))
    assert report["added"] == ["A"]
    assert report["skipped"][0]["name"] == "B"
    assert len(store["profiles"]) == agent_profiles.MAX_PROFILES


def test_an_unknown_mode_is_refused(store):
    with pytest.raises(ValueError, match="mode"):
        agent_profile_transfer.import_profiles(_doc(), mode="upsert")


def test_unavailable_models_are_kept_with_a_warning(store):
    report = agent_profile_transfer.import_profiles(
        _doc({"name": "M", "model": "gpt-elsewhere", "model_fallbacks": ["ok-model"]}),
        check_model=lambda spec: "not configured" if spec == "gpt-elsewhere" else None)
    assert store["profiles"][0]["model"] == "gpt-elsewhere"
    assert report["warnings"] == ["M: model 'gpt-elsewhere' is not available here (not configured)"]


# ── the agent tool ───────────────────────────────────────────────────────────

def _policy(**overrides):
    known = {"bash", "read_file", "write_file", "grep"}
    base = {"allowed_tools": set(known), "known_tools": set(known), "skill_names": set(),
            "allowed_models": set(), "memory_access": "write", "skill_access": "all", "model_access": "all",
            "allowed_mcp_servers": ["*"], "private_vault_access": True, "delegation_policy": "auto",
            "max_parallel_workers": 8, "approval_mode": "auto"}
    base.update(overrides)
    return base


@pytest.fixture
def tool_store(store, monkeypatch):
    holder = {"policy": _policy()}
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: holder["policy"])
    monkeypatch.setattr(loadout_tools, "_model_problem", lambda spec, owner: None)
    store["policy"] = holder
    return store


async def test_tool_export_returns_the_document(tool_store):
    tool_store["profiles"] = [_profile("Reviewer"), _profile("Writer")]
    result = await manage_agent_loadout('{"action": "export", "names": ["Writer"]}', "c", owner="u")
    assert result["exit_code"] == 0
    assert result["document"]["format"] == "odysseus-agent-profiles"
    assert [p["name"] for p in result["document"]["profiles"]] == ["Writer"]
    missing = await manage_agent_loadout('{"action": "export", "names": ["Ghost"]}', "c", owner="u")
    assert missing["exit_code"] == 1


async def test_tool_import_clamps_each_profile_to_the_calling_chat(tool_store):
    tool_store["policy"]["policy"] = _policy(allowed_tools={"read_file", "grep"}, private_vault_access=False,
                                             memory_access="read", approval_mode="ask_all")
    doc = _doc({"name": "Wide", "tool_access": "selected", "enabled_tools": ["bash", "grep"],
                "private_vault_access": True, "memory_access": "write", "approval_mode": "auto"})
    result = await manage_agent_loadout(json.dumps({"action": "import", "document": doc}), "c", owner="u")
    assert result["exit_code"] == 0, result
    stored = tool_store["profiles"][0]
    assert stored["enabled_tools"] == ["grep"]
    assert stored["private_vault_access"] is False
    assert stored["memory_access"] == "read"
    assert stored["approval_mode"] == "ask_all"
    assert result["report"]["narrowed"]["Wide"]


async def test_tool_import_refuses_what_create_refuses(tool_store):
    doc = _doc({"name": "Everything", "tool_access": "all"},
               {"name": "Nothing", "tool_access": "selected", "enabled_tools": ["send_email"]})
    result = await manage_agent_loadout(json.dumps({"action": "import", "document": json.dumps(doc)}),
                                        "c", owner="u")
    assert result["exit_code"] == 1
    assert tool_store["profiles"] == []
    errors = {e["name"]: e["error"] for e in result["report"]["errors"]}
    assert "tool_access 'all'" in errors["Everything"]
    assert "none of its tools" in errors["Nothing"]


async def test_tool_import_rejects_a_bad_document(tool_store):
    result = await manage_agent_loadout(
        json.dumps({"action": "import", "document": {"format": "x", "version": 1, "profiles": []}}), "c", owner="u")
    assert result["exit_code"] == 1 and "format" in result["error"]


def test_export_is_classified_as_a_read_and_import_as_a_write():
    from src.tool_capabilities import ToolEffect, action_is_read, capabilities_for_action

    export = capabilities_for_action("manage_agent_loadout", '{"action": "export"}')
    assert action_is_read("manage_agent_loadout", '{"action": "export"}')
    assert ToolEffect.WRITE_PRIVATE not in export.effects
    for action in ("import", "create"):
        caps = capabilities_for_action("manage_agent_loadout", json.dumps({"action": action}))
        assert ToolEffect.WRITE_PRIVATE in caps.effects
        assert not action_is_read("manage_agent_loadout", json.dumps({"action": action}))
    assert (capabilities_for_action("manage_agent_loadout", '{"action": "import"}').effects
            == capabilities_for_action("manage_agent_loadout", '{"action": "create"}').effects)


def test_ask_all_stops_an_import_but_not_an_export():
    from src.approval_modes import change_reason

    assert change_reason("manage_agent_loadout", '{"action": "import"}')
    assert change_reason("manage_agent_loadout", '{"action": "export"}') is None


def test_tool_schema_lists_the_new_actions():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS as TOOL_SCHEMAS

    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "manage_agent_loadout")
    props = schema["function"]["parameters"]["properties"]
    assert {"export", "import"} <= set(props["action"]["enum"])
    assert set(props["action"]["enum"]) == set(loadout_tools._ACTIONS)
    assert props["mode"]["enum"] == ["merge", "replace"]
    assert {"names", "document", "rename_conflicts"} <= set(props)


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client(store, monkeypatch):
    import src.auth_helpers as helpers
    import src.tool_security as security
    from routes.agents_routes import setup_agents_routes

    auth = {"admin": True, "token": False}
    monkeypatch.setattr(helpers, "require_user", lambda request: "alice")
    monkeypatch.setattr(helpers, "is_delegated_credential", lambda request: auth["token"])
    monkeypatch.setattr(security, "owner_is_admin_or_single_user", lambda user: auth["admin"])
    monkeypatch.setattr(loadout_tools, "_model_problem", lambda spec, owner: None)

    class _Sessions:
        def get_sessions_for_user(self, user):
            return {}

    app = FastAPI()
    app.include_router(setup_agents_routes(_Sessions()))
    test_client = TestClient(app)
    test_client.gate = auth
    return test_client


def test_export_route_downloads_json(client, store):
    store["profiles"] = [_profile("Reviewer"), _profile("Writer")]
    res = client.get("/api/agents/profiles/export?names=Reviewer")
    assert res.status_code == 200
    assert res.headers["content-disposition"].startswith('attachment; filename="odysseus-agent-profiles-')
    assert [p["name"] for p in res.json()["profiles"]] == ["Reviewer"]
    assert client.get("/api/agents/profiles/export?names=Ghost").status_code == 404


def test_import_route_accepts_wrapped_and_bare_documents(client, store):
    store["profiles"] = [_profile("Old")]
    res = client.post("/api/agents/profiles/import",
                      json={"document": _doc({"name": "New"}), "mode": "replace"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["report"]["removed"] == ["Old"]
    assert [p["name"] for p in body["profiles"]] == ["New"]

    res = client.post("/api/agents/profiles/import?rename_conflicts=true", json=_doc({"name": "New"}))
    assert res.json()["report"]["added"] == ["New 2"]


def test_import_route_rejects_a_foreign_document(client, store):
    res = client.post("/api/agents/profiles/import", json={"format": "other", "version": 1, "profiles": []})
    assert res.status_code == 400 and "format" in res.json()["detail"]
    assert store["writes"] == 0


@pytest.mark.parametrize("who", ["non_admin", "api_token"])
@pytest.mark.parametrize("method,path", [("get", "/api/agents/profiles/export"),
                                         ("post", "/api/agents/profiles/import")])
def test_profile_transfer_routes_are_admin_only(client, store, who, method, path):
    if who == "non_admin":
        client.gate["admin"] = False
    else:
        client.gate["token"] = True
    kwargs = {"json": _doc({"name": "Sneaky"})} if method == "post" else {}
    res = getattr(client, method)(path, **kwargs)
    assert res.status_code == 403
    assert store["writes"] == 0


# ── the loadout library UI (Agent Control Room › Loadouts) ─────────────────────


# ── templates: {server:<name>} references (2026-10-01) ─────────────────────────

class _FakeManager:
    def __init__(self, connected=None):
        self._configs = {sid: {"name": name} for sid, name in (connected or {}).items()}
        self._statuses = {sid: {"status": "connected"} for sid in (connected or {})}

    def get_all_statuses(self):
        return dict(self._statuses)

    def get_server_status(self, sid):
        return self._statuses.get(sid, {"status": "disconnected"})


@pytest.fixture
def no_saved_servers(monkeypatch):
    # Saved MCP rows live in the app database; keep the resolver off it.
    import core.database as database

    class _Q:
        def all(self):
            return []

    class _S:
        def query(self, *_a):
            return _Q()

        def close(self):
            pass

    monkeypatch.setattr(database, "SessionLocal", lambda: _S())


def _template(*entries, servers=("{server:penpot}",)):
    return _doc({"name": "Designer", "tool_access": "selected", "enabled_tools": list(entries),
                 "mcp_access": "selected", "allowed_mcp_servers": list(servers)})


def test_server_references_resolve_to_the_matching_servers_id(no_saved_servers):
    doc = _template("grep", "mcp__{server:penpot}__create_frame", "mcp__{server:Penpot}__*")
    resolved, notes = agent_profile_transfer.resolve_template(doc, _FakeManager({"c5ec6d7a": "Penpot"}))
    profile = resolved["profiles"][0]
    assert profile["enabled_tools"] == ["grep", "mcp__c5ec6d7a__create_frame", "mcp__c5ec6d7a__*"]
    assert profile["allowed_mcp_servers"] == ["c5ec6d7a"]
    assert notes == []
    # The input is left alone.
    assert doc["profiles"][0]["allowed_mcp_servers"] == ["{server:penpot}"]


def test_builtin_ids_and_unique_partial_names_resolve(no_saved_servers):
    doc = _template("mcp__{server:penpot_studio}__*", "mcp__{server:Penpot}__x",
                    servers=("{server:penpot_studio}", "{server:penpot}"))
    resolved, notes = agent_profile_transfer.resolve_template(doc, _FakeManager({"abc123": "Penpot MCP"}))
    profile = resolved["profiles"][0]
    assert profile["enabled_tools"] == ["mcp__penpot_studio__*", "mcp__abc123__x"]
    assert profile["allowed_mcp_servers"] == ["penpot_studio", "abc123"]
    assert notes == []


def test_unresolvable_references_are_dropped_with_a_note(no_saved_servers):
    doc = _template("grep", "mcp__{server:nothing}__a", "mcp__{server:nothing}__b", servers=("{server:nothing}",))
    resolved, notes = agent_profile_transfer.resolve_template(doc, _FakeManager())
    profile = resolved["profiles"][0]
    assert profile["enabled_tools"] == ["grep"] and profile["allowed_mcp_servers"] == []
    assert len(notes) == 1 and "'nothing'" in notes[0] and "3 entries" in notes[0]


def test_import_applies_templates_and_reports_what_was_dropped(store, no_saved_servers):
    doc = _template("mcp__{server:penpot}__create_frame", "mcp__{server:gone}__x",
                    servers=("{server:penpot}", "{server:gone}"))
    report = agent_profile_transfer.import_profiles(doc, manager=_FakeManager({"c5ec6d7a": "Penpot"}))
    assert report["written"] and not report["errors"]
    stored = store["profiles"][0]
    assert stored["enabled_tools"] == ["mcp__c5ec6d7a__create_frame"]
    assert stored["allowed_mcp_servers"] == ["c5ec6d7a"]
    assert any("'gone'" in w for w in report["warnings"])


def test_shipped_penpot_template_has_no_install_specific_ids():
    path = ROOT / "integrations/penpot/loadouts/penpot-product-designer.json"
    text = path.read_text(encoding="utf-8")
    assert "c5ec6d7a" not in text and "{server:penpot}" in text
    doc = json.loads(text)
    assert doc["profiles"][0]["allowed_mcp_servers"] == ["{server:penpot}", "penpot_studio"]


def test_templates_are_listed_and_installed_for_available_integrations(store, no_saved_servers, monkeypatch):
    from src import integration_registry

    monkeypatch.setattr(integration_registry, "available_ids", lambda manager=None: {"penpot"})
    rows = agent_profile_transfer.list_templates()
    assert [(r["integration"], r["template"], r["installed"]) for r in rows] == [
        ("penpot", "penpot-product-designer", False)]
    manager = _FakeManager({"c5ec6d7a": "Penpot"})
    report = agent_profile_transfer.install_template("penpot", "penpot-product-designer", manager=manager)
    assert report["added"] == ["Penpot Product Designer"]
    assert "mcp__c5ec6d7a__create_frame" in store["profiles"][0]["enabled_tools"]
    assert agent_profile_transfer.list_templates()[0]["installed"] is True
    with pytest.raises(ValueError, match="already exists"):
        agent_profile_transfer.install_template("penpot", "penpot-product-designer", manager=manager)
    agent_profile_transfer.install_template("penpot", "penpot-product-designer", manager=manager, overwrite=True)
    with pytest.raises(ValueError, match="no loadout template"):
        agent_profile_transfer.install_template("penpot", "../../etc/passwd", manager=manager)
    monkeypatch.setattr(integration_registry, "available_ids", lambda manager=None: set())
    assert agent_profile_transfer.list_templates() == []


def test_template_routes_use_the_import_gate(client, store, no_saved_servers, monkeypatch):
    from src import integration_registry

    monkeypatch.setattr(integration_registry, "available_ids", lambda manager=None: {"penpot"})
    res = client.get("/api/agents/profiles/templates")
    assert res.status_code == 200 and res.json()["templates"][0]["template"] == "penpot-product-designer"
    body = {"integration": "penpot", "template": "penpot-product-designer"}
    assert client.post("/api/agents/profiles/templates/install", json=body).status_code == 200
    assert client.post("/api/agents/profiles/templates/install", json=body).status_code == 409
    assert client.post("/api/agents/profiles/templates/install",
                       json={**body, "template": "nope"}).status_code == 404
    client.gate["admin"] = False
    assert client.get("/api/agents/profiles/templates").status_code == 403
    assert client.post("/api/agents/profiles/templates/install", json=body).status_code == 403


