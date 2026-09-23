"""Per-chat settings: what a chat remembers about how it runs.

Two kinds of keys live in ``sessions.settings_json``:

* **Policy** the server enforces on every turn: ``approval_mode`` (see
  :mod:`src.tool_approvals`), ``tool_access``/``enabled_tools`` (this chat's
  tool allowlist, stored as an allowlist and inverted where it is evaluated —
  :func:`src.tool_policy.allowlist_permits`) and ``disabled_tools`` (tools
  switched off for this chat only, on top of the global and per-user
  denylists). ``tool_access`` is absent on chats written before allowlists were
  stored, and resolves to ``"all"`` — those chats keep being governed by the
  ``disabled_tools`` their loadout wrote at the time.
* **Last used** state the frontend restores when the chat is reopened:
  ``toggles`` (agent/chat mode, web, shell, plan, knowledge base),
  ``workspace`` and ``preset_id``. The chat route records these from each turn,
  so a chat keeps its own setup instead of inheriting whatever the previous
  chat left in the browser.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from src import tool_approvals

_TOGGLE_KEYS = ("web", "bash", "plan", "rag")
MAX_DISABLED_TOOLS = 300
PRIVATE_VAULT_ACCESS_KEY = "private_vault_access"
_ACCESS_MODES = frozenset({"none", "read", "write"})
_SELECTION_MODES = frozenset({"all", "selected", "none"})
_MODEL_ACCESS_MODES = frozenset({"current", "selected", "all"})
_DELEGATION_POLICIES = frozenset({"never", "explicit", "auto"})
_LIST_KEYS = frozenset({"skill_names", "allowed_models", "allowed_mcp_servers", "enabled_tools"})


def validate_patch(patch: Any) -> Dict[str, Any]:
    """Return the accepted subset of a settings update, or raise ValueError."""
    if not isinstance(patch, dict):
        raise ValueError("settings must be a JSON object")
    out: Dict[str, Any] = {}
    for key, value in patch.items():
        if key == "approval_mode":
            if value is None:
                out[key] = None
            elif value not in tool_approvals.MODES:
                raise ValueError(f"approval_mode must be one of {', '.join(tool_approvals.MODES)}")
            else:
                out[key] = value
        elif key == "disabled_tools":
            if value is None:
                out[key] = None
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError("disabled_tools must be a list of tool names")
            names = sorted({v.strip() for v in value})
            if len(names) > MAX_DISABLED_TOOLS:
                raise ValueError("too many disabled_tools")
            out[key] = names or None
        elif key == "toggles":
            if value is None:
                out[key] = None
                continue
            if not isinstance(value, dict):
                raise ValueError("toggles must be an object")
            toggles: Dict[str, Any] = {}
            mode = value.get("mode")
            if mode is not None:
                if mode not in ("chat", "agent"):
                    raise ValueError("toggles.mode must be chat or agent")
                toggles["mode"] = mode
            for name in _TOGGLE_KEYS:
                if name in value and value[name] is not None:
                    toggles[name] = bool(value[name])
            out[key] = toggles or None
        elif key in ("workspace", "preset_id"):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            out[key] = (value or "").strip()[:1000] or None
        elif key == PRIVATE_VAULT_ACCESS_KEY:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            out[key] = value
        elif key == "memory_access":
            if value not in _ACCESS_MODES:
                raise ValueError(f"memory_access must be one of {', '.join(sorted(_ACCESS_MODES))}")
            out[key] = value
        elif key == "tool_access":
            if value not in _SELECTION_MODES:
                raise ValueError(f"tool_access must be one of {', '.join(sorted(_SELECTION_MODES))}")
            out[key] = value
        elif key == "skill_access":
            if value not in _SELECTION_MODES:
                raise ValueError(f"skill_access must be one of {', '.join(sorted(_SELECTION_MODES))}")
            out[key] = value
        elif key == "model_access":
            if value not in _MODEL_ACCESS_MODES:
                raise ValueError(f"model_access must be one of {', '.join(sorted(_MODEL_ACCESS_MODES))}")
            out[key] = value
        elif key == "delegation_policy":
            if value not in _DELEGATION_POLICIES:
                raise ValueError(f"delegation_policy must be one of {', '.join(sorted(_DELEGATION_POLICIES))}")
            out[key] = value
        elif key in _LIST_KEYS:
            if value is None:
                out[key] = []
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError(f"{key} must be a list of names")
            out[key] = sorted({v.strip() for v in value})[:300]
        elif key == "max_parallel_workers":
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise ValueError("max_parallel_workers must be a number")
            out[key] = max(0, min(8, count))
        elif key == "agent_profile":
            if value is not None and not isinstance(value, str):
                raise ValueError("agent_profile must be a string")
            out[key] = (value or "").strip()[:40] or None
        else:
            raise ValueError(f"unknown setting {key!r}")
    return out


def stored_disabled_tools(settings: Optional[Dict[str, Any]]) -> Set[str]:
    """The tools this chat has switched off, read off the chat's own settings.

    The single reader of the stored tool-denial shape. Two places need exactly
    the same answer: the chat route before a live turn, and
    :func:`src.headless_agent.run_headless` when a chat continues *itself*
    headlessly (after a worker finishes, or after a background job does). They
    had no shared definition, and the second one simply did not do it — which
    is how a worker finishing came to run the user's chat with none of the
    chat's own denials in force. One definition, imported: when the stored shape
    grows another form (an allowlist stored as an allowlist rather than as an
    inverted denylist), extend it here and both callers follow.

    Owner-level denials are deliberately NOT included —
    :func:`src.tool_security.owner_baseline_disabled_tools` owns those, and
    every agent turn merges the two.
    """
    names = (settings or {}).get("disabled_tools") or []
    if not isinstance(names, list):
        # Policy fails closed, but there is no safe non-empty guess to make from
        # a malformed row: say "nothing is recorded here" and let the owner
        # baseline (which is read from a different store) still apply.
        return set()
    return {str(name).strip() for name in names if str(name).strip()}


def effective_approval_mode(settings: Optional[Dict[str, Any]]) -> str:
    """The chat's approval mode, else the global default, else ``auto``."""
    mode = (settings or {}).get("approval_mode")
    if mode in tool_approvals.MODES:
        return mode
    try:
        from src.settings import get_setting

        return tool_approvals.normalize_mode(get_setting("agent_approval_mode", tool_approvals.DEFAULT_MODE))
    except Exception:
        return tool_approvals.DEFAULT_MODE


def last_used_from_request(*, chat_mode: str, allow_web: Any, allow_bash: Any, plan_mode: bool,
                           use_rag: Any, workspace: Optional[str], preset_id: Optional[str]) -> Dict[str, Any]:
    """The ``toggles``/``workspace``/``preset_id`` patch describing one turn."""
    def truthy(value):
        return None if value is None else str(value).strip().lower() == "true"

    toggles = {"mode": chat_mode if chat_mode in ("chat", "agent") else None,
               "web": truthy(allow_web), "bash": truthy(allow_bash), "plan": bool(plan_mode),
               # use_rag is only ever sent as "false" (unchecked); absent means on.
               "rag": False if str(use_rag or "").lower() == "false" else True}
    return {"toggles": {k: v for k, v in toggles.items() if v is not None},
            "workspace": workspace or None, "preset_id": preset_id or None}
