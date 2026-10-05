"""Plugin catalog v2 (2026-10-01): package format for user-added integrations.

Covers the manifest (v1 still loads, v2 validation and caps), the install flow
(server row, draft skills, loadouts, exact uninstall), MCP server instructions
and the registry's plugin integration.
"""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import plugin_routes as pr
from src import agent_loadouts, agent_profiles, integration_registry as reg, plugin_catalog
from src.mcp_manager import MAX_SERVER_INSTRUCTIONS_CHARS, McpManager, clean_server_instructions
from src.plugin_catalog import PluginCatalog, PluginManifestError, validate_manifest

SKILL = "---\nname: demo-howto\ndescription: How to use the demo server\n---\n\n# Demo\n\nCall demo_list first.\n"


def v1():
    return {"schema_version": 1, "id": "old", "name": "Old", "version": "1",
            "capabilities": {"skills": [], "mcp_servers": [], "tools": ["web_search"], "models": []}}


def v2(**integration):
    block = {
        "name": "Demo",
        "mcp_server": {"name": "Demo Server", "transport": "stdio", "command": "npx",
                       "args": ["-y", "demo-mcp"],
                       "env": [{"name": "DEMO_TOKEN", "description": "API token"}]},
        "instructions": "Call demo_list before demo_get.",
        "skills": [{"name": "demo-howto", "content": SKILL}],
        "loadout_templates": [{"format": "odysseus-agent-profiles", "version": 1, "profiles": [
            {"name": "Demo agent", "instructions": "Use the demo server.",
             "allowed_mcp_servers": ["{server:Demo Server}"]}]}],
    }
    block.update(integration)
    return {"schema_version": 2, "id": "demo", "name": "Demo pack", "version": "1.0",
            "capabilities": {}, "integration": block}


# -- manifest ---------------------------------------------------------------


def test_v1_manifest_still_loads_unchanged(tmp_path):
    saved = PluginCatalog(tmp_path).save(v1())
    assert saved["schema_version"] == 1 and "integration" not in saved


def test_v1_cannot_carry_an_integration_block():
    raw = v1()
    raw["integration"] = v2()["integration"]
    with pytest.raises(PluginManifestError, match="schema_version 2"):
        validate_manifest(raw)


def test_v2_round_trip(tmp_path):
    catalog = PluginCatalog(tmp_path)
    saved = catalog.save(v2())
    assert catalog.get("demo") == saved
    assert saved["integration"]["mcp_server"]["env"] == [
        {"name": "DEMO_TOKEN", "description": "API token", "required": True}]


@pytest.mark.parametrize("mutate,match", [
    (lambda b: b["mcp_server"].update(transport="websocket"), "transport"),
    (lambda b: b["mcp_server"].update(env={"DEMO_TOKEN": "sk-live-123"}), "never values"),
    (lambda b: b["mcp_server"].update(env=["DEMO_TOKEN=sk-live-123"]), "each mcp_server.env entry"),
    (lambda b: b["mcp_server"].update(env=[{"name": "DEMO_TOKEN", "value": "sk-live-123"}]), "each mcp_server.env entry"),
    (lambda b: b["mcp_server"].update(env=[{"name": "LD_PRELOAD"}]), "may not set"),
    (lambda b: b["mcp_server"].update(command=""), "command is required"),
    (lambda b: b["mcp_server"].update(url="https://x.example"), "not url"),
    (lambda b: b.update(mcp_server={"name": "S", "transport": "http", "url": "https://u:p@x.example/mcp"}), "credentials"),
    (lambda b: b.update(mcp_server={"name": "S", "transport": "http", "url": "ftp://x.example"}), "http"),
    (lambda b: b.update(skills=[{"name": "Bad Name", "content": "x"}]), "skill names"),
    (lambda b: b.update(skills=[{"name": "big", "content": "x" * (plugin_catalog.MAX_SKILL_CHARS + 1)}]), "larger than"),
    (lambda b: b.update(skills=[{"name": f"s{i}", "content": "x"} for i in range(11)]), "at most"),
    (lambda b: b.update(instructions="x" * (plugin_catalog.MAX_INSTRUCTIONS_CHARS + 1)), "at most"),
    (lambda b: b.update(instructions="a\x00b"), "control characters"),
    (lambda b: b.update(loadout_templates=[{"format": "other"}]), "version 1 document"),
    (lambda b: b.update(hooks={"post_install": "sh"}), "unknown integration"),
    (lambda b: b["mcp_server"].update(run="sh"), "unknown mcp_server"),
])
def test_v2_validation_rejects(mutate, match):
    raw = v2()
    mutate(raw["integration"])
    with pytest.raises(PluginManifestError, match=match):
        validate_manifest(raw)


@pytest.mark.parametrize("name", [
    "BASH_ENV", "ENV", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "PERL5OPT", "RUBYOPT",
    "GIT_SSH_COMMAND", "NPM_CONFIG_PREFIX", "NODE_EXTRA_CA_CERTS", "PYTHONINSPECT",
    "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "HTTPS_PROXY", "SSL_CERT_FILE", "ODYSSEUS_DATA_DIR",
    "HOME", "PATH",
])
def test_v2_refuses_env_that_changes_how_code_runs(name):
    # 2026-10-01 security review: the first denylist named eight variables.
    raw = v2()
    raw["integration"]["mcp_server"]["env"] = [{"name": name}]
    with pytest.raises(PluginManifestError, match="may not set|upper-case"):
        validate_manifest(raw)


@pytest.mark.parametrize("name", ["api_key", "Api_Key", "http_proxy"])
def test_v2_env_names_are_upper_case(name):
    raw = v2()
    raw["integration"]["mcp_server"]["env"] = [{"name": name}]
    with pytest.raises(PluginManifestError, match="upper-case"):
        validate_manifest(raw)


@pytest.mark.parametrize("name", ["DEMO_TOKEN", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "API_BASE_URL"])
def test_v2_server_settings_are_still_allowed(name):
    raw = v2()
    raw["integration"]["mcp_server"]["env"] = [{"name": name}]
    validate_manifest(raw)


def test_instructions_need_a_server():
    raw = v2()
    del raw["integration"]["mcp_server"]
    with pytest.raises(PluginManifestError, match="need an mcp_server"):
        validate_manifest(raw)


def test_large_v2_manifest_loads_but_large_v1_does_not(tmp_path):
    big = v2(skills=[{"name": f"s{i}", "content": "x" * 20000} for i in range(5)])
    catalog = PluginCatalog(tmp_path)
    assert len(json.dumps(big)) > plugin_catalog.MAX_MANIFEST_BYTES
    catalog.save(big)
    assert catalog.get("demo")["schema_version"] == 2
    (tmp_path / "old.json").write_text(json.dumps({**v1(), "description": "x" * 70000}), encoding="utf-8")
    with pytest.raises(PluginManifestError, match="too large"):
        catalog.get("old")


# -- install flow -----------------------------------------------------------


class FakeManager:
    _configs = {"srv12345": {"name": "Demo Server"}}

    def get_all_statuses(self):
        return {"srv12345": {"status": "connected"}}

    def get_server_status(self, sid):
        return {"status": "connected"} if sid == "srv12345" else {"status": "disconnected"}

    def get_all_tools(self, *_a, **_k):
        return []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from services.memory.skills import SkillsManager

    monkeypatch.setattr(plugin_catalog, "PLUGIN_DIR", tmp_path / "plugins")
    store: list = []
    monkeypatch.setattr(agent_profiles, "load_profiles", lambda: [dict(p) for p in store])
    monkeypatch.setattr(agent_loadouts, "_write",
                        lambda profiles: store.__setitem__(slice(None), agent_profiles.validate_profiles(profiles)))
    monkeypatch.setattr(pr, "require_admin", lambda request: None)
    monkeypatch.setattr(pr, "effective_user", lambda request: "alice")
    import src.tool_utils as tool_utils
    monkeypatch.setattr(tool_utils, "get_mcp_manager", lambda: FakeManager())
    added, removed = [], []

    async def add(request, spec, env_values):
        added.append((spec, env_values))
        return {"id": "srv12345", "name": spec["name"], "connected": True, "status": "connected", "tool_count": 2}

    async def remove(request, server_id):
        removed.append(server_id)

    catalog = PluginCatalog()
    skills = SkillsManager(str(tmp_path / "data"))
    router = pr.setup_plugin_routes(catalog, session_manager=object(), mcp_add=add, mcp_remove=remove,
                                    skills_manager=skills)
    eps = {(m, r.path): r.endpoint for r in router.routes for m in r.methods}
    return SimpleNamespace(catalog=catalog, skills=skills, store=store, added=added, removed=removed, eps=eps)


def _req(body=None):
    async def json_body():
        return body
    return SimpleNamespace(json=json_body)


INSTALL = ("POST", "/api/plugins/{plugin_id}/install")
UNINSTALL = ("DELETE", "/api/plugins/{plugin_id}/install")
APPROVED = "npx -y demo-mcp"


@pytest.mark.asyncio
async def test_install_creates_server_draft_skills_and_loadouts_then_uninstall_removes_exactly_those(env):
    env.catalog.save(v2())
    # A loadout and a skill the admin made by hand must survive uninstall.
    env.skills.add_skill(name="mine", description="d", owner="alice", procedure=["x"])
    agent_loadouts._write([{"name": "Mine", "instructions": "keep"}])

    report = await env.eps[INSTALL](_req({"env": {"DEMO_TOKEN": "sk-secret"}, "approved": APPROVED}), "demo")

    assert env.added[0][0]["command"] == "npx" and env.added[0][1] == {"DEMO_TOKEN": "sk-secret"}
    assert report["server"]["id"] == "srv12345"
    assert report["skills"] == [{"name": "demo-howto", "status": "draft"}]
    assert report["loadouts"] == ["Demo agent"]
    assert report["unresolved_references"] == []
    skill = next(s for s in env.skills.load_all() if s["name"] == "demo-howto")
    assert skill["status"] == "draft" and skill["source"] == "imported" and skill["owner"] == "alice"
    assert skill["requires_integration"] == "demo"
    assert [p["name"] for p in env.store] == ["Mine", "Demo agent"]
    assert env.store[1]["allowed_mcp_servers"] == ["srv12345"]
    record = env.catalog.install_record("demo")
    assert "sk-secret" not in json.dumps(record) and "sk-secret" not in json.dumps(report)

    with pytest.raises(HTTPException) as exc:
        await env.eps[INSTALL](_req({"env": {"DEMO_TOKEN": "x"}, "approved": APPROVED}), "demo")
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await env.eps[("DELETE", "/api/plugins/{plugin_id}")](_req(), "demo")
    assert exc.value.status_code == 409

    gone = await env.eps[UNINSTALL](_req(), "demo")
    assert gone["removed"] == {"server": "srv12345", "skills": ["demo-howto"], "loadouts": ["Demo agent"]}
    assert env.removed == ["srv12345"]
    assert [p["name"] for p in env.store] == ["Mine"]
    names = {s["name"] for s in env.skills.load_all()}
    assert "demo-howto" not in names and "mine" in names
    assert env.catalog.install_record("demo") is None


@pytest.mark.asyncio
async def test_publish_skills_is_explicit(env):
    env.catalog.save(v2())
    report = await env.eps[INSTALL](
        _req({"env": {"DEMO_TOKEN": "t"}, "approved": APPROVED, "publish_skills": True}), "demo")
    assert report["skills"][0]["status"] == "published"


@pytest.mark.asyncio
async def test_install_refuses_without_the_reviewed_command_and_bad_env(env):
    env.catalog.save(v2())
    for body, code in [
        ({"env": {"DEMO_TOKEN": "t"}}, 409),
        ({"env": {"DEMO_TOKEN": "t"}, "approved": "npx -y something-else"}, 409),
        ({"env": {}, "approved": APPROVED}, 400),  # required name missing
        ({"env": {"DEMO_TOKEN": "t", "EXTRA": "x"}, "approved": APPROVED}, 400),  # undeclared name
        ({"env": {"DEMO_TOKEN": "a\nb"}, "approved": APPROVED}, 400),
    ]:
        with pytest.raises(HTTPException) as exc:
            await env.eps[INSTALL](_req(body), "demo")
        assert exc.value.status_code == code
    assert env.added == [] and env.catalog.install_record("demo") is None


@pytest.mark.asyncio
async def test_failed_skill_import_rolls_the_server_back(env):
    env.catalog.save(v2(skills=[{"name": "ok", "content": SKILL}, {"name": "broken", "content": "no frontmatter at all"}]))
    # Make the second skill fail the way a bad bundle does.
    real = env.skills.import_bundle_from_files
    calls = []

    def flaky(files, **kw):
        calls.append(files)
        if len(calls) == 2:
            raise ValueError("bad bundle")
        return real(files, **kw)

    env.skills.import_bundle_from_files = flaky
    with pytest.raises(HTTPException) as exc:
        await env.eps[INSTALL](_req({"env": {"DEMO_TOKEN": "t"}, "approved": APPROVED}), "demo")
    assert exc.value.status_code == 400 and "rolled back" in exc.value.detail
    assert env.removed == ["srv12345"]
    assert not any(s["name"] == "demo-howto" for s in env.skills.load_all())
    assert env.catalog.install_record("demo") is None


@pytest.mark.asyncio
async def test_unresolved_template_references_are_reported(env, monkeypatch):
    from src import agent_profile_transfer
    if not hasattr(agent_profile_transfer, "resolve_template"):
        pytest.skip("resolve_template has not landed")
    doc = v2()["integration"]["loadout_templates"][0]
    doc["profiles"][0]["allowed_mcp_servers"] = ["{server:Nothing Like It}"]
    env.catalog.save(v2(loadout_templates=[doc]))
    report = await env.eps[INSTALL](_req({"env": {"DEMO_TOKEN": "t"}, "approved": APPROVED}), "demo")
    assert any("Nothing Like It" in n for n in report["unresolved_references"])


@pytest.mark.asyncio
async def test_v1_plugin_cannot_be_installed(env):
    env.catalog.save(v1())
    with pytest.raises(HTTPException) as exc:
        await env.eps[INSTALL](_req({}), "old")
    assert exc.value.status_code == 400


def test_installed_server_is_granted_when_the_plugin_is_enabled(env):
    env.catalog.save(v2())
    env.catalog.write_install_record({"plugin_id": "demo", "server_id": "srv12345"})
    eff = pr._effective(env.catalog.get("demo"), env.catalog.install_record("demo"))
    assert eff["capabilities"]["mcp_servers"] == ["srv12345"]


# -- registry ---------------------------------------------------------------


def test_registry_lists_installed_plugin_as_an_integration(env):
    env.catalog.write_install_record({"plugin_id": "demo", "name": "Demo", "server_id": "srv12345",
                                      "instructions": "Call demo_list first."})
    builtin_before = set(reg.function_calling_server_ids()), reg.is_builtin("srv12345")
    items = reg.plugin_integrations()
    assert [(i.id, i.kind, i.server_id) for i in items] == [("demo", "plugin", "srv12345")]
    assert "demo" in reg.available_ids(FakeManager())
    assert "demo" not in reg.available_ids(SimpleNamespace(get_server_status=lambda s: {"status": "error"}))
    assert reg.integration_for_tool("mcp__srv12345__demo_list") == "demo"
    assert reg.integration_for_tool("mcp__other__x") is None
    assert reg.plugin_instructions("srv12345") == "Call demo_list first."
    # Built-in behaviour is untouched by an installed plugin.
    assert (set(reg.function_calling_server_ids()), reg.is_builtin("srv12345")) == builtin_before
    assert all(i.kind != "plugin" for i in reg.all())


# -- MCP server instructions ------------------------------------------------


def _mgr(tools_by_server, instructions):
    mgr = McpManager()
    for sid, tools in tools_by_server.items():
        mgr._tools[sid] = [{"name": t, "description": f"{t} tool", "input_schema": {}} for t in tools]
        mgr._connections[sid] = {"status": "connected", "name": f"Server {sid}"}
    for sid, text in instructions.items():
        mgr._set_server_instructions(sid, SimpleNamespace(instructions=text))
    return mgr


def test_instructions_are_captured_bounded_and_cleaned():
    mgr = _mgr({"a": ["t"]}, {"a": "Call t first.\x00\r\n" + "z" * 10000})
    text = mgr.server_instructions("a")
    assert len(text) <= MAX_SERVER_INSTRUCTIONS_CHARS and "\x00" not in text and "\r" not in text
    assert mgr.server_instructions("nope") == ""
    assert clean_server_instructions(None) == "" and clean_server_instructions(5) == ""
    mgr._set_server_instructions("a", SimpleNamespace(instructions=None))
    assert mgr.server_instructions("a") == ""


def test_instructions_show_only_for_offered_servers_and_keep_the_prefix_stable():
    mgr = _mgr({"a": ["alpha"], "b": ["beta"]}, {"a": "Call alpha first.", "b": "Beta secret rules."})
    shown = mgr.get_tool_descriptions_for_prompt({"b": {"beta"}})  # b's only tool is switched off
    assert "Call alpha first." in shown and "untrusted" in shown
    assert "Beta secret rules." not in shown
    assert mgr.get_tool_descriptions_for_prompt({"b": {"beta"}}) == shown  # stable across calls
    both = mgr.get_tool_descriptions_for_prompt({})
    assert "Beta secret rules." in both
    plain = _mgr({"a": ["alpha"]}, {}).get_tool_descriptions_for_prompt({})
    assert "Server instructions" not in plain


def test_new_instructions_bust_the_prompt_cache():
    mgr = _mgr({"a": ["alpha"]}, {"a": "old"})
    assert "old" in mgr.get_tool_descriptions_for_prompt({})
    mgr._set_server_instructions("a", SimpleNamespace(instructions="new"))
    out = mgr.get_tool_descriptions_for_prompt({})
    assert "new" in out and "old" not in out


def test_plugin_instructions_appear_beside_the_installed_servers_tools(env):
    env.catalog.write_install_record({"plugin_id": "demo", "name": "Demo", "server_id": "a",
                                      "instructions": "Plugin says: list first."})
    out = _mgr({"a": ["alpha"]}, {}).get_tool_descriptions_for_prompt({})
    assert "Plugin notes" in out and "Plugin says: list first." in out
