"""Per-chat settings: what a chat remembers about how it runs.

Two kinds of keys live in ``sessions.settings_json``:

* **Policy** the server enforces on every turn: ``approval_mode`` (see
  :mod:`src.tool_approvals`) and ``disabled_tools`` (tools switched off for
  this chat only, on top of the global and per-user denylists).
* **Last used** state the frontend restores when the chat is reopened:
  ``toggles`` (agent/chat mode, web, shell, plan, knowledge base),
  ``workspace`` and ``preset_id``. The chat route records these from each turn,
  so a chat keeps its own setup instead of inheriting whatever the previous
  chat left in the browser.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src import tool_approvals

_TOGGLE_KEYS = ("web", "bash", "plan", "rag")
MAX_DISABLED_TOOLS = 300


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
        else:
            raise ValueError(f"unknown setting {key!r}")
    return out


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
