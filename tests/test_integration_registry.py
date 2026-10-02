"""Integration manifests and the registry (2026-10-01).

The hand-kept lists in builtin_mcp / mcp_manager were replaced by values derived
from integrations/*/integration.json. The first tests pin today's membership so
the move provably changed nothing; the rest cover validation, status and lookups.
"""

import json

import pytest

from src import builtin_mcp, integration_registry as reg
from src.mcp_manager import McpManager, _BUILTIN_FUNCTION_CALLING_SERVERS

# Copied from the pre-registry source (2026-09-30), not computed from it.
OLD_SERVERS = {
    "image_gen": ("mcp_servers/image_gen_server.py", "Built-in: Image Generation"),
    "memory": ("mcp_servers/memory_server.py", "Built-in: Memory"),
    "rag": ("mcp_servers/rag_server.py", "Built-in: RAG"),
    "email": ("mcp_servers/email_server.py", "Built-in: Email"),
    "todoist": ("mcp_servers/todoist_server.py", "Built-in: Todoist"),
    "lotus": ("mcp_servers/lotus_server.py", "Built-in: Lotus"),
    "pi_worker": ("mcp_servers/pi_worker_server.py", "Built-in: Windows Pi Worker"),
    "penpot_studio": ("mcp_servers/penpot_studio_server.py", "Built-in: Penpot Studio"),
}
OLD_FUNCTION_CALLING = {
    "builtin_browser", "todoist", "lotus", "pi_worker", "github_read", "github_write", "penpot_studio",
}
OLD_IS_BUILTIN = {
    "image_gen", "memory", "rag", "email", "todoist", "lotus", "pi_worker",
    "github_read", "github_write", "penpot_studio",
}
OLD_CATALOG_IDS = [
    "memory", "rag", "email", "image_gen", "lotus", "todoist", "pi_worker",
    "penpot_studio", "builtin_browser", "github_read", "github_write",
]
OLD_READ_TOOLS = (
    "get_file_contents", "get_commit", "list_branches", "list_commits", "search_code",
    "issue_read", "search_issues", "list_pull_requests", "pull_request_read",
    "actions_list", "actions_get", "get_job_logs", "get_me", "list_issues", "search_pull_requests",
)
OLD_WRITE_TOOLS = (
    "create_pull_request", "add_issue_comment", "pull_request_review_write",
    "add_comment_to_pending_review",
)


def test_derived_lists_equal_the_old_hand_kept_ones():
    assert builtin_mcp._BUILTIN_SERVERS == OLD_SERVERS
    assert builtin_mcp._BUILTIN_NPX_SERVERS == {
        "builtin_browser": {
            "name": "Built-in: Browser",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest", "--headless", "--caps", "vision"],
        }
    }
    assert _BUILTIN_FUNCTION_CALLING_SERVERS == OLD_FUNCTION_CALLING
    assert {sid for sid in OLD_IS_BUILTIN | {"builtin_browser"} if reg.is_builtin(sid)} == OLD_IS_BUILTIN | {"builtin_browser"}
    manager = McpManager.__new__(McpManager)
    for sid in OLD_IS_BUILTIN | {"builtin_browser", "builtin_anything"}:
        assert manager.is_builtin(sid)
    for sid in ("c5ec6d7a", "user_server", ""):
        assert not manager.is_builtin(sid)
    assert [e["id"] for e in builtin_mcp.BUILTIN_CATALOG] == OLD_CATALOG_IDS
    assert builtin_mcp.GITHUB_MCP_READ_TOOLS == OLD_READ_TOOLS
    assert builtin_mcp.GITHUB_MCP_WRITE_TOOLS == OLD_WRITE_TOOLS


def test_catalog_text_is_unchanged():
    rows = {r["id"]: r for r in builtin_mcp.BUILTIN_CATALOG}
    assert rows["rag"]["name"] == "Knowledge (RAG)"
    assert rows["pi_worker"]["name"] == "Windows Pi worker"
    assert rows["github_read"]["name"] == "GitHub (read)"
    assert rows["github_write"]["description"].startswith("Comments, reviews and edits")


def test_github_servers_match_the_old_definitions(monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("ODYSSEUS_GITHUB_MCP_WRITE", "1")
    monkeypatch.setattr(builtin_mcp, "find_github_mcp_binary", lambda: "/bin/gh-mcp")
    assert builtin_mcp.github_mcp_servers() == {
        "github_read": {
            "name": "Built-in: GitHub Read", "command": "/bin/gh-mcp",
            "args": ["stdio", "--read-only", "--tools=" + ",".join(OLD_READ_TOOLS)],
        },
        "github_write": {
            "name": "Built-in: GitHub Write", "command": "/bin/gh-mcp",
            "args": ["stdio", "--tools=" + ",".join(OLD_WRITE_TOOLS)],
        },
    }


def test_every_shipped_manifest_loads_and_ids_are_unique():
    ids = [i.id for i in reg.all()]
    assert sorted(ids) == sorted([
        "memory", "rag", "image_gen", "email", "todoist", "lotus", "pi-worker", "penpot",
        "browser", "github", "claude-code", "cookbook",
    ])
    assert reg.get("claude-code").capability == "code_delegation"
    assert reg.get("cookbook").capability == "model_serving"
    assert reg.get("penpot").servers[0].id == "penpot_studio"


@pytest.mark.parametrize("patch, message", [
    ({"kind": "wasm"}, "kind must be one of"),
    ({"servers": []}, "needs at least one server"),
    ({"requirements": [{"name": "x", "check": {"type": "psychic"}}]}, "check"),
    ({"health": {"type": "vibes"}}, "health.type"),
    ({"tools": "everything"}, "tools must be a list"),
])
def test_a_malformed_manifest_names_the_problem(tmp_path, patch, message):
    manifest = {
        "id": "x", "name": "X", "description": "d", "kind": "mcp-python",
        "servers": [{"id": "x", "name": "Built-in: X", "script": "mcp_servers/x.py"}],
    }
    manifest.update(patch)
    path = tmp_path / "integration.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(reg.IntegrationManifestError, match=message):
        reg._parse(path)


def test_native_manifest_must_name_a_capability(tmp_path):
    path = tmp_path / "integration.json"
    path.write_text(json.dumps({"id": "n", "name": "N", "description": "d", "kind": "native"}), encoding="utf-8")
    with pytest.raises(reg.IntegrationManifestError, match="capability"):
        reg._parse(path)


def test_pi_worker_is_only_started_with_its_host_variable(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_PI_WORKER_HOST", raising=False)
    assert not reg.startable("pi_worker")
    monkeypatch.setenv("ODYSSEUS_PI_WORKER_HOST", "win.lan")
    assert reg.startable("pi_worker")
    # Todoist has always started without its token; only its status changes.
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
    assert reg.startable("todoist")


class _Manager:
    def __init__(self, connected):
        self.connected = set(connected)

    def get_server_status(self, sid):
        return {"status": "connected" if sid in self.connected else "disconnected"}


def test_connected_without_credentials_is_not_configured(monkeypatch):
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
    manager = _Manager({"todoist"})
    st = reg.status("todoist", manager)
    assert st["connected"] and not st["configured"]
    assert [m["requirement"] for m in st["missing"]] == ["TODOIST_API_TOKEN"]
    assert "todoist" not in reg.available_ids(manager)
    monkeypatch.setenv("TODOIST_API_TOKEN", "t")
    assert "todoist" in reg.available_ids(manager)
    assert reg.status("todoist", _Manager(set()))["connected"] is False


def test_penpot_requirement_accepts_env_or_the_saved_mcp_row(monkeypatch):
    for var in ("PENPOT_API_URL", "PENPOT_BASE_URL", "PENPOT_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    from src import penpot_studio

    def not_configured():
        raise penpot_studio.PenpotError("no")

    monkeypatch.setattr(penpot_studio, "load_config", not_configured)
    assert not reg.status("penpot", _Manager({"penpot_studio"}))["configured"]
    monkeypatch.setenv("PENPOT_API_URL", "http://penpot.lan")
    monkeypatch.setenv("PENPOT_ACCESS_TOKEN", "t")
    assert reg.status("penpot", _Manager({"penpot_studio"}))["configured"]
    monkeypatch.delenv("PENPOT_ACCESS_TOKEN")
    monkeypatch.setattr(penpot_studio, "load_config", lambda: object())  # the borrowed row
    assert reg.status("penpot", _Manager({"penpot_studio"}))["configured"]


def test_github_write_needs_the_flag_the_token_and_the_binary(monkeypatch):
    monkeypatch.setattr(builtin_mcp, "find_github_mcp_binary", lambda: "/bin/gh-mcp")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_x")
    monkeypatch.delenv("ODYSSEUS_GITHUB_MCP_WRITE", raising=False)
    assert reg.server_status("github_read")["configured"]
    assert not reg.server_status("github_write")["configured"]
    monkeypatch.setenv("ODYSSEUS_GITHUB_MCP_WRITE", "1")
    assert reg.server_status("github_write")["configured"]
    monkeypatch.setattr(builtin_mcp, "find_github_mcp_binary", lambda: "")
    assert not reg.server_status("github_read")["configured"]


def test_tools_resolve_to_their_integration():
    assert reg.integration_for_tool("mcp__penpot_studio__penpot_render") == "penpot"
    assert reg.integration_for_tool("mcp__github_read__get_me") == "github"
    assert reg.integration_for_tool("mcp__builtin_browser__browser_navigate") == "browser"
    assert reg.integration_for_tool("delegate_to_claude_code") == "claude-code"
    assert reg.integration_for_tool("serve_model") == "cookbook"
    assert reg.integration_for_tool("mcp__c5ec6d7a__anything") is None
    assert reg.integration_for_tool("read_file") is None


def test_skill_and_loadout_lookups_tolerate_empty_and_unknown():
    assert reg.skill_dirs("browser") == [] and reg.loadout_templates("browser") == []
    # Penpot ships a skill package and a loadout template (2026-10-01).
    assert [p.name for p in reg.skill_dirs("penpot")] == ["skills"]
    assert [p.name for p in reg.loadout_templates("penpot")] == ["penpot-product-designer.json"]
    assert reg.skill_dirs("nope") == [] and reg.loadout_templates("nope") == []


def test_native_integrations_follow_their_capability(monkeypatch):
    from src import capabilities

    monkeypatch.setattr(capabilities, "status", lambda name: capabilities.CapabilityStatus(
        name=name, title=name, summary="", enabled=True, satisfied=True))
    assert {"claude-code", "cookbook"} <= reg.available_ids(_Manager(set()))
    monkeypatch.setattr(capabilities, "status", lambda name: capabilities.CapabilityStatus(
        name=name, title=name, summary="", enabled=False, satisfied=True))
    assert not {"claude-code", "cookbook"} & reg.available_ids(_Manager(set()))
