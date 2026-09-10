"""tool_types.py — the two names every layer of the tool pipeline needs.

``ToolBlock`` and ``TOOL_TAGS`` used to live in ``src/agent_tools/__init__.py``,
which made them expensive to reach: parsing and schema code had to import the
whole tool registry to name a tuple, and the registry imports parsing and schema
code straight back. That cycle resolved only when the cluster was entered via
``src.agent_tools`` — so the day something imported ``src.tool_schemas`` first,
the app died at startup with a partially-initialized-module ImportError.

This module is a leaf. It imports one frozen set from ``tool_security`` and
nothing else, so any module may import it at any point without creating a cycle.
``src.agent_tools`` re-exports both names, so existing importers are unaffected.
"""

from collections import namedtuple

from src.tool_security import BUILTIN_EMAIL_TOOLS

# Tool types that trigger execution
TOOL_TAGS = {"bash", "python", "web_search", "web_fetch", "read_file", "write_file", "edit_file",
             "apply_patch", "todowrite",
             # Isolated agent worktree + human-gated publishing, and read-only
             # access to the app's own logs for self-debugging.
             "manage_agent_worktree", "read_app_logs",
             "grep", "glob", "ls", "get_workspace", "manage_bg_jobs",
             "create_document", "update_document", "edit_document",
             "search_chats", "search_documents", "recall_tool_output",
             "chat_with_model", "create_session", "list_sessions",
             "send_to_session",
             "pipeline",
             "manage_session", "manage_memory", "list_models",
             "ui_control", "generate_image", "ask_user", "update_plan",
             "manage_tasks", "api_call", "ask_teacher", "manage_skills",
             "suggest_document",
             "manage_endpoints", "manage_mcp", "manage_webhooks",
             "manage_tokens", "manage_documents", "manage_settings",
             "manage_notes", "manage_calendar", "manage_wellbeing",
             "resolve_contact", "manage_contact", "delegate_to_claude_code",
             # Email tool names come from BUILTIN_EMAIL_TOOLS (unioned below)
             # so the fence regex, dispatch, and non-admin blocklist all cover
             # the same set.
             # Cookbook tools (LLM serving + downloads). Without these
             # entries, native function calls to e.g. list_served_models
             # are rejected as "Unknown function call" before reaching
             # the dispatcher — silent failure for the whole cookbook
             # surface.
             "download_model", "serve_model",
             "list_served_models", "stop_served_model",
             "list_downloads", "cancel_download",
             "search_hf_models", "list_cached_models",
             "list_serve_presets", "serve_preset", "adopt_served_model",
             "list_cookbook_servers",
             # Other tools the agent reaches for that were also missing.
             "edit_image", "trigger_research", "manage_research",
             # Generic loopback to any UI-button endpoint (cookbook,
             # gallery, email folders, etc.) — agent uses this when
             # there's no named tool wrapper for the action.
             "app_api"} | BUILTIN_EMAIL_TOOLS

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])
