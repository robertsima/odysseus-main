"""``manage_agent_loadout`` — let an agent define and start worker loadouts.

Creating a loadout is a policy action, so the interesting part is not the CRUD:
it is :mod:`src.agent_loadouts`, which intersects whatever the agent asks for
with the calling chat's own policy before anything is stored. The tool's job is
to parse arguments, refuse actions the caller's delegation policy forbids, and
report back exactly which parts of a request were narrowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src import agent_loadouts, agent_profiles
from src.worker_preflight import WorkerBlocked
from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("list", "get", "capabilities", "preflight", "create", "update", "delete", "start", "status", "stop",
            "export", "import")
# Spellings models use for "how is that worker doing". 2026-09-26: gpt-6-luna
# called {"action": "poll", "run_id": ...} (delegate_to_claude_code's verb) and
# got "action must be one of ...". Kept out of `_ACTIONS`, which is the schema
# enum, so the schema still teaches the one canonical name.
_ACTION_ALIASES = {"poll": "status", "wait": "status", "check": "status"}

# A status wait blocks this tool call, not a model round: a parent that wants
# to wait for its worker spends one call instead of one ~100k-token round per
# check. Bounded like delegate_to_claude_code's poll wait.
MAX_STATUS_WAIT_S = 300
_STATUS_WAIT_POLL_SECONDS = 2.0
# Repeated status checks on the same run(s) soon after the last one get an
# automatic wait, doubling to a ceiling (claude_code_tools._poll_wait's rule):
# the 2026-09-26 parent re-checked a running worker in a tight loop.
_STATUS_BACKOFF_START_S = 15
_STATUS_BACKOFF_MAX_S = 120
_STATUS_REPEAT_WINDOW_S = 600
_last_status_checks: Dict[str, Tuple[float, int]] = {}
# Fields an agent may set. `name` is required; everything else falls back to
# agent_profiles' own defaults.
_FIELDS = (
    "name", "description", "instructions", "persona_name", "temperature", "max_tokens", "reasoning_effort",
    "model", "model_fallbacks", "model_access",
    "allowed_models", "tool_access", "enabled_tools", "disabled_tools", "memory_access",
    "skill_access", "skill_names", "mcp_access", "allowed_mcp_servers",
    "private_vault_access", "shell_access", "approval_mode", "delegation_policy",
    "max_parallel_workers", "max_rounds",
)


# Numeric fields whose valid range starts at 1, so a supplied 0 is a provider
# filling in a blank rather than a setting. `max_parallel_workers` is pointedly
# NOT here: 0 is a real value there and means "this worker may start no
# children of its own".
# `temperature`/`max_tokens` join them: a filled-in 0 would pin a loadout to
# greedy sampling, and max_tokens 0 already means "server decides".
_ZERO_MEANS_UNSET = frozenset({"max_rounds", "temperature", "max_tokens"})


def _is_blank(key: str, value: Any) -> bool:
    """Whether a supplied field carries no instruction from the caller.

    Native function-calling providers fill every property they were shown, so a
    real call arrives as `{"action": "update", "name": "X", "instructions": "",
    "model": "", "tool_access": "", "max_rounds": 0, ...}` — one intended edit
    and a dozen blanks. Treating those blanks as values is what made an update
    destructive. `False` is a real setting and is never blank.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    if key in _ZERO_MEANS_UNSET and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value <= 0
    return False


def _tool_examples(policy: Dict[str, Any], limit: int = 14) -> str:
    names = sorted(policy["allowed_tools"])
    if not names:
        return "no tools at all (this chat's own tool access is empty)"
    shown = ", ".join(names[:limit])
    return f"{len(names)} tool(s), e.g. {shown}" if len(names) > limit else shown


# How many tool names a log line or a tool result spells out before switching
# to a count. A worker granted 200 tools produced a 200-name wall in the server
# log and again in the model's context (2026-09-17); the names past the first
# dozen tell nobody anything.
_TOOL_LIST_PREVIEW = 12


def _tool_list_note(tools: Any) -> str:
    if isinstance(tools, str):
        return tools
    names = [str(t) for t in (tools or [])]
    if not names:
        return "none"
    shown = ", ".join(names[:_TOOL_LIST_PREVIEW])
    if len(names) <= _TOOL_LIST_PREVIEW:
        return shown
    return f"{shown} … (+{len(names) - _TOOL_LIST_PREVIEW} more, {len(names)} total)"


# Shorthand an agent may put in `enabled_tools`: the read-only tools this chat
# can grant, resolved at authoring time. It is also what a loadout gets when
# its author names no tool policy at all.
READ_ONLY_TOKEN = "@read_only"


def _scope_tools(requested: Dict[str, Any], policy: Dict[str, Any], *, action: str,
                 supplied_access: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Settle an agent-authored tool policy before the clamp sees it.

    Returns ``(error, notes)``. The clamp only narrows to what the caller has,
    which for ``tool_access: "all"`` is *everything* the caller has: two
    read-only audit loadouts were stored on 2026-09-17 with ~200 tools each —
    bash, python, send_email, vault_unlock, manage_settings, Bluesky posting,
    the whole browser MCP surface — because the model wrote "all" and nothing
    asked it what the task needed. So:

    - "all" is refused, with the read-only set and the mutating tools it would
      have granted, so the author names what the worker needs;
    - no tool policy at all defaults to the read-only set the chat can grant
      (a note says so), falling back to the caller's own tools only when the
      chat has no read-only tools to give — a loadout that grants nothing is
      the failure the starved-loadout check exists for, not an outcome;
    - ``@read_only`` inside ``enabled_tools`` expands to that same set.
    """
    allowed = set(policy["allowed_tools"])
    read_only = sorted(allowed & agent_loadouts.read_only_tools())
    notes: List[str] = []
    if supplied_access == "all":
        mutating = sorted(allowed - set(read_only))
        return {
            "error": (
                f"{action}: tool_access 'all' would hand this worker every one of the "
                f"{len(allowed)} tools this chat has, including {len(mutating)} that write, send, "
                f"post or run things ({_tool_list_note(mutating[:8]) if mutating else 'none'}). "
                "Name what the task needs instead: tool_access='selected' with enabled_tools "
                f"listing exact names, or enabled_tools=[\"{READ_ONLY_TOKEN}\"] for the "
                f"{len(read_only)} read-only tools ({_tool_list_note(read_only)}) plus any others "
                "by name. action='capabilities' with detail=true lists every grantable name."
            ),
            "read_only_tools": read_only,
            "mutating_tool_count": len(mutating),
            "exit_code": 1,
        }, notes
    enabled = requested.get("enabled_tools")
    if isinstance(enabled, list) and READ_ONLY_TOKEN in enabled:
        requested["enabled_tools"] = sorted(
            {str(t) for t in enabled if t != READ_ONLY_TOKEN} | set(read_only))
        requested.setdefault("tool_access", "selected")
        notes.append(f"tools: {READ_ONLY_TOKEN} expanded to {len(read_only)} read-only tool(s)")
    if not requested.get("tool_access"):
        if read_only:
            requested["tool_access"] = "selected"
            requested["enabled_tools"] = sorted(set(requested.get("enabled_tools") or []) | set(read_only))
            notes.append(
                f"tools: no tool policy given, so this loadout gets the {len(read_only)} read-only "
                f"tool(s) this chat can grant ({_tool_list_note(read_only)}); name enabled_tools "
                "to widen it"
            )
        else:
            notes.append("tools: no tool policy given and this chat has no read-only tools, "
                         "so the loadout gets the chat's own tools")
    return None, notes


def _one_off_profile(profile: Dict[str, Any], extra: List[str],
                     policy: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """``profile`` plus ``extra`` tools, for one worker run.

    Returns ``(one_off, granted, refused)``. Each extra tool goes through the
    same clamp ``create`` uses, so a run can get only tools this chat may use
    itself; the rest of the loadout is exactly as the user saved it. ``one_off``
    is None when the loadout already grants every tool (``tool_access: all``).
    """
    probe, _notes = agent_loadouts.clamp(
        {"name": profile["name"], "tool_access": "selected", "enabled_tools": list(extra)}, policy)
    kept = set(probe.get("enabled_tools") or [])
    granted = [t for t in extra if t in kept]
    refused = [t for t in extra if t not in kept]
    if (profile.get("tool_access") or "all") == "all" or not granted:
        return None, granted, refused
    released = agent_profiles.expand_tool_aliases(set(granted))
    one_off = dict(profile)
    one_off["tool_access"] = "selected"
    one_off["enabled_tools"] = sorted(set(profile.get("enabled_tools") or []
                                          if profile.get("tool_access") == "selected" else []) | set(granted))
    one_off["disabled_tools"] = [t for t in profile.get("disabled_tools") or [] if t not in released]
    return agent_profiles.validate_profiles([one_off])[0], granted, refused


def _model_problem(spec: str, owner: Optional[str]) -> Optional[str]:
    """Why ``spec`` cannot be started with right now, or None.

    Checked when a loadout is written, not when it is started: `model:
    "gpt-luna-5.6"` (a misspelling of gpt-5.6-luna) was accepted at create and
    only failed at start with "has no available model", two rounds later.
    A registry that cannot be read is not the author's mistake, so that is
    not a problem here.
    """
    try:
        from src.ai_interaction import _resolve_model

        _resolve_model(spec, owner=owner)
    except ValueError as exc:
        return str(exc)
    except Exception:  # registry unavailable: leave it to start
        logger.debug("loadout: model check for %r skipped", spec, exc_info=True)
    return None


def _available_model_ids(owner: Optional[str], limit: int = 40) -> List[str]:
    """Model ids the author could have meant, from every enabled endpoint."""
    ids: List[str] = []
    try:
        from src.auth_helpers import owner_filter
        from src.database import ModelEndpoint, SessionLocal
        from src.endpoint_resolver import build_headers, resolve_endpoint_runtime
        from src.llm_core import list_model_ids

        db = SessionLocal()
        try:
            query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner:
                query = owner_filter(query, ModelEndpoint, owner)
            endpoints = list(query.all())
        finally:
            db.close()
        for ep in endpoints:
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
                ids.extend(list_model_ids(base, timeout=5, headers=build_headers(api_key, base),
                                          owner=owner, endpoint_id=getattr(ep, "id", None)))
            except Exception:
                continue
    except Exception:
        logger.debug("loadout: could not list model ids", exc_info=True)
    seen: List[str] = []
    for model_id in ids:
        if model_id and model_id not in seen:
            seen.append(model_id)
    return seen[:limit]


# Which fields a narrowing note is about, by its label. An update that supplies
# none of them did not ask for that change.
_NOTE_FIELDS = {
    "tools": {"tool_access", "enabled_tools", "disabled_tools"},
    "models": {"model", "model_fallbacks", "model_access", "allowed_models"},
    "model": {"model", "model_fallbacks", "model_access", "allowed_models"},
    "allowed_models": {"model", "model_fallbacks", "model_access", "allowed_models"},
    "model_fallbacks": {"model", "model_fallbacks", "model_access", "allowed_models"},
    "mcp_access": {"mcp_access", "allowed_mcp_servers", "tool_access", "enabled_tools"},
    "allowed_mcp_servers": {"mcp_access", "allowed_mcp_servers", "tool_access", "enabled_tools"},
    "skill_access": {"skill_access", "skill_names"},
    "skill_names": {"skill_access", "skill_names"},
}


def _unrequested_narrowings(notes: List[str], supplied: Dict[str, Any]) -> List[str]:
    """Narrowings an ``update`` would apply to fields the call never touched.

    The clamp narrows the WHOLE merged loadout to the calling chat's ceiling,
    so a worker that only edited Lead Engineer's instructions also rewrote its
    delegation auto -> explicit and its worker limit 2 -> 1 (2026-09-24) —
    the user's settings, silently lowered because a narrower chat saved it.
    """
    out = []
    for note in notes:
        label = note.split(":", 1)[0].strip()
        if "tool allowlist can reach" in note:
            continue  # normalisation of the stored shape, not the caller's ceiling
        fields = _NOTE_FIELDS.get(label, {label})
        if not (fields & set(supplied)):
            out.append(note)
    return out


def _supplied(args: Dict[str, Any]) -> Dict[str, Any]:
    """The loadout fields this call actually carries, given inline or under `loadout`."""
    nested = args.get("loadout")
    source = nested if isinstance(nested, dict) else args
    return {key: source[key] for key in _FIELDS
            if key in source and not _is_blank(key, source[key])}


def _requested(args: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The loadout definition, given either inline or under `loadout`.

    With `base` (an update), the result is that stored profile with only the
    fields the caller actually supplied overridden. Without it (a create), it is
    just the supplied fields, and `validate_profiles` fills the rest with
    defaults.

    Blanks are ignored rather than written, so an untouched field keeps its
    stored value instead of resetting to a default — for `tool_access` that
    default is "all", which turned "rename this loadout" into "re-grant it every
    tool its author can use". A field is unset deliberately by naming it in
    `clear`, never by sending it empty.
    """
    supplied = _supplied(args)
    if base is None:
        return supplied
    merged = {key: base[key] for key in _FIELDS if key in base}
    merged.update(supplied)
    raw_clear = args.get("clear")
    for key in raw_clear if isinstance(raw_clear, (list, tuple)) else []:
        if key in _FIELDS and key != "name":
            merged.pop(str(key), None)
    return merged


def _release_stale_denials(requested: Dict[str, Any], base: Dict[str, Any], supplied: Dict[str, Any],
                           required_tools: Any) -> List[str]:
    """Let an update grant a tool the stored ``disabled_tools`` snapshot names.

    A stored selected-tools loadout carries ``disabled_tools`` = every tool it
    did NOT grant when it was saved (``agent_loadouts._clamp_tools``' complement
    snapshot). An update that adds a tool to ``enabled_tools`` merged that
    snapshot back in, and the clamp removed the new tool again because it was
    "denied" — silently, with exit_code 0 and a READY readiness. That is how
    Lead Engineer was "repaired" to include manage_git on 2026-09-26 and still
    started workers without it.

    Only when this call names the tools (``enabled_tools`` or
    ``required_tools``) and leaves ``disabled_tools`` alone; a call that sends
    ``disabled_tools`` states it outright. The clamp still caps every name at
    what this chat may grant. Returns the notes to report.
    """
    if "disabled_tools" in supplied or requested.get("tool_access") != "selected":
        return []
    named = set(supplied.get("enabled_tools") or []) | {str(t) for t in (required_tools or [])}
    if not named:
        return []
    granted = agent_profiles.expand_tool_aliases(set(requested.get("enabled_tools") or []) & named)
    stored = list(requested.get("disabled_tools") or base.get("disabled_tools") or [])
    released = sorted(t for t in stored if t in granted)
    if not released:
        return []
    requested["disabled_tools"] = [t for t in stored if t not in granted]
    return [f"tools: now granting {', '.join(released[:8])}{'…' if len(released) > 8 else ''}, "
            "which the stored disabled_tools had denied"]


# ── widening a saved loadout needs the user ─────────────────────────────────
#
# An agent may narrow a saved loadout, or fix its wording, as part of a task.
# It may not widen one — more tools, more access — just because one task
# needed more: the loadout is the user's standing decision about what that
# worker may do. The Settings editor (routes/auth_routes.py, agent_profiles)
# is the user's own path and is not affected; this tool is only ever called
# by an agent. A widening goes through when the user asked for it in this
# chat: their latest message names the loadout and the tools (or says to
# add/grant access), or it answers "yes" to an ask_user that named the loadout.

# Messages a person typed: no source (the chat composer) or a steer from the
# Agents panel. Worker results, peer agents and dashboard tasks are not.
_HUMAN_SOURCES = frozenset({"", "user", "steer"})
_WIDEN_INTENT_RE = re.compile(
    r"\b(?:add|grant|give|allow|enable|widen|expand|extend|let)\b[^.\n]{0,80}"
    r"\b(?:tools?|access|permissions?|capabilit\w+)\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_RE = re.compile(
    r"^\W*(?:yes|yep|yeah|y|sure|ok(?:ay)?|approved?|go ahead|do it|confirm(?:ed)?|allow(?: it)?|"
    r"grant(?: it)?|please do)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(r"\b(?:no|not|don'?t|never|deny|refuse|cancel)\b", re.IGNORECASE)


# "the preset", "this loadout": the user pointing at the loadout the chat has
# just been talking about instead of naming it. 2026-09-28 18: "grant the
# preset delegate_to_claude_code", right after a reply about Lead Engineer.
_LOADOUT_REFERENCE_RE = re.compile(
    r"\b(?:the|this|that|its|it'?s)\s+(?:preset|loadout|profile|agent|worker)\b", re.IGNORECASE)
_GRANT_VERB_RE = re.compile(r"\b(?:add|grant|give|allow|enable|let)\b", re.IGNORECASE)


def _is_human_message(message: Any) -> bool:
    if _field(message, "role") != "user":
        return False
    meta = _field(message, "metadata") or {}
    return str(meta.get("source") or "") in _HUMAN_SOURCES and meta.get("kind") != "peer"


def _names_tool(tool: str, lowered: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(tool.casefold()) + r"(?![\w])", lowered))


def _assistant_text(message: Any) -> str:
    """What an assistant message said, including the questions it asked."""
    parts = [str(_field(message, "content") or "")]
    for event in (_field(message, "metadata") or {}).get("tool_events") or []:
        if isinstance(event, dict) and event.get("tool") == "ask_user":
            parts.append(f"{event.get('command') or ''} {event.get('output') or ''}")
    return " ".join(parts)


class _Authorization:
    """Whether the person in this chat asked for this widening, and if not, why."""

    def __init__(self, ok: bool, *, worker: bool = False, unnamed: Optional[List[str]] = None,
                 marker: Any = None):
        self.ok, self.worker, self.unnamed, self.marker = ok, worker, list(unnamed or []), marker


def _chat_history(session_id: Optional[str], owner: Optional[str]) -> Tuple[bool, Optional[List[Any]]]:
    """``(is_worker, history)`` for the calling chat; history None when unreadable."""
    if not session_id:
        return False, None
    try:
        from core.database import get_session_settings

        if (get_session_settings(session_id) or {}).get("parent_session"):
            return True, None  # a worker has no user of its own to ask
    except Exception:
        pass
    try:
        from src.ai_interaction import get_session_manager

        manager = get_session_manager()
        sess = manager.get_session(session_id) if manager else None
    except Exception:
        return False, None
    if sess is None or (owner and getattr(sess, "owner", None) != owner):
        return False, None
    return False, list(getattr(sess, "history", None) or [])


def _widening_authorization(session_id: Optional[str], owner: Optional[str], loadout_name: str,
                            added_tools: List[str]) -> _Authorization:
    """Did the person in this chat ask for this loadout to gain ``added_tools``?

    Yes when their latest message (typed by them, not a worker result):

    - names the loadout and every added tool ("Giving you explicit
      authorization to add manage_git to the Lead Engineer preset"), or names
      the loadout and asks to widen it in general words without naming tools;
    - says "the preset"/"this loadout", names every added tool with a grant
      verb, and the chat's reply just before it named the loadout ("grant the
      preset delegate_to_claude_code");
    - answers yes to an ask_user that named the loadout, or to a reply that
      named the loadout and every added tool.

    Naming some tools is consent to those only: an update that also adds
    tools the user did not name is refused, naming them (``unnamed``).
    """
    is_worker, history = _chat_history(session_id, owner)
    if is_worker:
        return _Authorization(False, worker=True)
    if history is None:
        return _Authorization(False)
    index = next((i for i in range(len(history) - 1, -1, -1) if _is_human_message(history[i])), None)
    if index is None:
        return _Authorization(False)
    text = str(_field(history[index], "content") or "")
    marker = (index, hash(text))
    lowered, name = text.casefold(), loadout_name.casefold()
    added = [str(t) for t in added_tools or []]
    named = [t for t in added if _names_tool(t, lowered)]
    unnamed = [t for t in added if t not in named]
    previous = next((m for m in reversed(history[:index]) if _field(m, "role") == "assistant"), None)
    previous_text = _assistant_text(previous).casefold() if previous is not None else ""

    def grant(names_loadout: bool, generic_ok: bool) -> Optional[_Authorization]:
        if not names_loadout:
            return None
        if named:
            return _Authorization(not unnamed, unnamed=unnamed, marker=marker)
        if generic_ok and _WIDEN_INTENT_RE.search(text):
            return _Authorization(True, marker=marker)
        return None

    # Named outright.
    decided = grant(bool(name) and name in lowered, generic_ok=True)
    if decided is None and added and _LOADOUT_REFERENCE_RE.search(text) and _GRANT_VERB_RE.search(text):
        # "the preset", resolved to the loadout the chat's last reply was about.
        decided = grant(bool(name) and name in previous_text, generic_ok=False)
    if decided is not None:
        return decided
    if not _AFFIRMATIVE_RE.search(text) or _NEGATIVE_RE.search(text) or previous is None:
        return _Authorization(False, marker=marker)
    # "yes" to the question the agent just asked about this loadout.
    for event in (_field(previous, "metadata") or {}).get("tool_events") or []:
        if isinstance(event, dict) and event.get("tool") == "ask_user":
            asked = f"{event.get('command') or ''} {event.get('output') or ''}".casefold()
            if name and name in asked:
                return _Authorization(True, marker=marker)
    reply = str(_field(previous, "content") or "").casefold()
    if name and name in reply and all(_names_tool(t, reply) for t in added):
        return _Authorization(True, marker=marker)
    return _Authorization(False, marker=marker)


def _user_authorized_widening(session_id: Optional[str], owner: Optional[str], loadout_name: str,
                              added_tools: List[str]) -> bool:
    """Whether the person in this chat asked for this loadout to be widened."""
    return _widening_authorization(session_id, owner, loadout_name, added_tools).ok


# {(chat, loadout): the user message a widening of it was last refused under}.
# A second widening of the same loadout under the same user message is the
# model retrying instead of asking: on 2026-09-28 the admin chat re-sent a
# refused Lead Engineer update six times in a row, each answered with the
# full refusal. Process-local and bounded; a restart only means the next
# repeat gets the full text once more.
_WIDENING_REFUSALS: Dict[Tuple[str, str], Tuple[Any, List[str]]] = {}
_WIDENING_REFUSALS_MAX = 256


def _refuse_widening(session_id: Optional[str], name: str, widened: Dict[str, Any],
                     auth: _Authorization) -> Dict[str, Any]:
    key = (str(session_id or ""), name.casefold())
    previous = _WIDENING_REFUSALS.get(key) if session_id and auth.marker is not None else None
    if previous is not None and previous[0] == auth.marker:
        refused = sorted(set(previous[1]) | set(widened["tools"]))
        _WIDENING_REFUSALS[key] = (auth.marker, refused)
        return _repeat_widening_refusal(name, widened, auth)
    if session_id and auth.marker is not None:
        if len(_WIDENING_REFUSALS) >= _WIDENING_REFUSALS_MAX:
            _WIDENING_REFUSALS.pop(next(iter(_WIDENING_REFUSALS)))
        _WIDENING_REFUSALS[key] = (auth.marker, list(widened["tools"]))
    return _widening_refusal(name, widened, auth)


def _what_it_gains(widened: Dict[str, Any]) -> str:
    tools = widened["tools"]
    return ", ".join(tools[:12]) if tools else "; ".join(widened["notes"])


def _widening_refusal(name: str, widened: Dict[str, Any],
                      auth: Optional[_Authorization] = None) -> Dict[str, Any]:
    auth = auth or _Authorization(False)
    tools = widened["tools"]
    gains = _what_it_gains(widened)
    if auth.worker:
        next_step = ("You are a worker: you cannot change a saved loadout. Say in your result that "
                     f"{name!r} would need {gains}, and let the chat that started you ask the user.")
    else:
        next_step = (f"Next step, once: ask the user with ask_user whether {name!r} should permanently gain "
                     f"{gains}. Do not retry this update until they answer; after a yes, send it once.")
    why = (f"the user named {', '.join(t for t in tools if t not in auth.unnamed)} but not "
           f"{', '.join(auth.unnamed)}" if auth.unnamed else "the user has not asked for that in this chat")
    extra = (f" For the task at hand you do not need the change: start the worker with "
             f"extra_tools={json.dumps(tools[:12])}, which applies to that one run only." if tools else "")
    return {
        "error": (
            f"update: {name!r} was not saved. It widens a saved loadout (" + "; ".join(widened["notes"])
            + f"), and {why}. " + next_step + extra
            + " Narrowing it, or changing its wording, model or round budget, needs no approval."
        ),
        "blocked": True,
        "blocked_reason": "update_would_widen_loadout",
        "would_widen": widened["notes"],
        **({"not_named_by_user": auth.unnamed} if auth.unnamed else {}),
        "next_action": ({"report_to_parent": True} if auth.worker else
                        {"ask_user": f"May I permanently give the {name} loadout {gains}?",
                         "then": "stop; do not retry the update until the user answers"}),
        **({"suggested_start": {"action": "start", "name": name, "extra_tools": tools}} if tools else {}),
        "exit_code": 1,
    }


def _repeat_widening_refusal(name: str, widened: Dict[str, Any], auth: _Authorization) -> Dict[str, Any]:
    """The same widening, again, with no new word from the user since the refusal.

    Short on purpose, and flagged ``repeat`` with a fixed ``progress_key`` so
    the agent loop's stall detector reads the retry as no progress and ends the
    tool loop instead of letting it spin.
    """
    gains = _what_it_gains(widened)
    step = ("report it to the chat that started you" if auth.worker else
            f"ask the user with ask_user (naming {name!r} and {gains}) or end your turn and say what needs "
            "their approval")
    return {
        "error": (f"update: {name!r} not saved — already refused this turn and the user has not answered. "
                  f"Do not send it again; {step}."),
        "blocked": True,
        "blocked_reason": "update_would_widen_loadout",
        "repeat": True,
        "would_widen": widened["notes"],
        "progress_key": f"widening-refused:{name.casefold()}",
        "exit_code": 1,
    }


def _caller_model(session_id: Optional[str]) -> str:
    """The calling chat's model: what a worker of a loadout naming none runs on."""
    if not session_id:
        return ""
    try:
        from src.ai_interaction import get_session_manager

        manager = get_session_manager()
        sess = manager.get_session(session_id) if manager else None
        return str(getattr(sess, "model", "") or "")
    except Exception:
        return ""


def _status_wait(key: str, requested: float) -> float:
    """Seconds this status check should wait: the caller's request, or the
    backoff floor when it re-checks the same runs soon after the last check."""
    now = time.monotonic()
    last = _last_status_checks.get(key)
    repeats = last[1] + 1 if last and now - last[0] < _STATUS_REPEAT_WINDOW_S else 0
    _last_status_checks[key] = (now, repeats)
    for stale in [k for k, (ts, _) in _last_status_checks.items() if now - ts > _STATUS_REPEAT_WINDOW_S]:
        _last_status_checks.pop(stale, None)
    if repeats == 0:
        return requested
    floor = min(_STATUS_BACKOFF_MAX_S, _STATUS_BACKOFF_START_S * 2 ** min(repeats - 1, 8))
    return max(requested, floor)


def _requested_wait(args: Dict[str, Any]) -> float:
    raw = args.get("wait_seconds")
    if raw is None and not isinstance(args.get("wait"), bool):
        raw = args.get("wait")
    try:
        return max(0.0, min(float(MAX_STATUS_WAIT_S), float(raw or 0)))
    except (TypeError, ValueError):
        return 0.0


def _field(message: Any, key: str) -> Any:
    return message.get(key) if isinstance(message, dict) else getattr(message, key, None)


# Run statuses whose worker has written its last word. A "blocked" run never
# started, so it has no result to store.
_FINISHED_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled", "waiting_approval"})
# How many finished runs one status call stores a full result for. Each is a
# disk write plus a background embedding; the rest keep their excerpt.
_MAX_STORED_RESULTS_PER_CALL = 5
# run_id -> {"ref", "chars", "status"}, so re-checking a finished run does not
# store its result again.
_RESULT_REFS: Dict[str, Dict[str, Any]] = {}


def _full_worker_text(worker_session: str, run_id: str) -> Optional[str]:
    """The worker's final message for ``run_id``, uncapped."""
    try:
        from src.ai_interaction import get_session_manager

        manager = get_session_manager()
        sess = manager.get_session(worker_session) if manager else None
    except Exception:
        return None
    for message in reversed(list(getattr(sess, "history", None) or [])):
        meta = _field(message, "metadata") or {}
        if _field(message, "role") == "assistant" and meta.get("run_id") == run_id:
            text = str(_field(message, "content") or "")
            return "" if text == "(no reply)" else text
    return None


def stored_worker_result(run_id: str, *, owner: Optional[str],
                         caller_session: Optional[str]) -> Optional[Dict[str, Any]]:
    """Put a finished worker's whole result in the tool-output store.

    ``{"ref", "chars", "status"}``, or None when the run has no result to
    store. 2026-09-28: the parent could not get a finished worker's result
    mid-turn — status gave a 400-character excerpt and the hand-off message
    was not in the running turn's context — so it asked both workers to
    repeat themselves with send_to_session, and they regenerated ~17k
    characters from memory, without tools, for five minutes. The result was
    in the worker's chat the whole time; this hands it over as a ref that
    recall_tool_output reads back.
    """
    from src import tool_output_store

    cached = _RESULT_REFS.get(run_id)
    if cached:
        if tool_output_store.load_record(cached["ref"]):
            return dict(cached)
        _RESULT_REFS.pop(run_id, None)  # pruned from the store: store it again
    try:
        from src import agent_control

        collected = agent_control.collect_worker_result(run_id, owner=owner)
    except Exception:
        return None
    text = str(collected.get("result") or "")
    if collected.get("result_truncated"):
        text = _full_worker_text(str(collected.get("session_id") or ""), run_id) or text
    if not text.strip():
        return None
    status = str(collected.get("status") or "")
    header = (f"[Worker result · run {run_id} · worker chat {collected.get('session_id')} · {status}. "
              "This is the worker's own report, not verified work.]\n\n")
    record = tool_output_store.store(header + text, tool="manage_agent_loadout",
                                     command=f"worker result {run_id}", session_id=caller_session)
    if not record:
        return None
    stored = {"ref": record["ref"], "chars": len(text), "status": status}
    _RESULT_REFS[run_id] = stored
    while len(_RESULT_REFS) > 500:
        _RESULT_REFS.pop(next(iter(_RESULT_REFS)))
    return dict(stored)


def finished_worker_result(worker_session: str, *, caller_session: Optional[str],
                           owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """The stored result of the latest finished run of ``worker_session``
    that ``caller_session`` started, or None. For send_to_session's hint."""
    if not worker_session or not caller_session:
        return None
    from src import agent_activity

    for run in agent_activity.list_runs(session_id=caller_session, limit=100, include_descendants=True):
        summary = run.get("summary") or {}
        if (summary.get("target_session") == worker_session and run.get("session_id") == worker_session
                and run.get("status") in _FINISHED_STATUSES):
            stored = stored_worker_result(run["run_id"], owner=owner, caller_session=caller_session)
            if stored:
                return {"run_id": run["run_id"], **stored}
            return None
    return None


def _status_rows(session_id: Optional[str], limit: int, run_id: str,
                 loadout: str = "") -> List[Dict[str, Any]]:
    from src import agent_activity

    rows = []
    wanted = loadout.strip().casefold()
    runs = agent_activity.list_runs(session_id=session_id,
                                    limit=max(limit, 100) if (run_id or wanted) else limit,
                                    include_descendants=True)
    for run in runs:
        if run_id and run.get("run_id") != run_id:
            continue
        summary = run.get("summary") or {}
        progress = run.get("progress") or {}
        # status with a loadout name reports that loadout's workers only. It
        # used to ignore the name and return up to 20 of the chat's runs, so
        # "status of Lead Engineer" came back as a page of unrelated failures
        # the model then took for the task at hand (2026-09-27).
        if wanted and str(summary.get("profile") or "").strip().casefold() != wanted:
            continue
        if len(rows) >= limit:
            break
        # A launch refused by preflight is filed under the chat that asked for
        # it and never had a chat of its own. Falling back to the run's
        # session_id reported the PARENT's id as the worker (2026-09-28).
        blocked = run["status"] == "blocked"
        row = {
            "run_id": run["run_id"], "status": run["status"], "title": run["title"],
            "worker_session": None if blocked else (summary.get("target_session") or run.get("session_id")),
            "note": "refused before start; no worker chat was created" if blocked else None,
            "loadout": summary.get("profile"), "model": summary.get("model"),
            "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
            "tool_calls": summary.get("steps") or (progress.get("tool_calls") if run["status"] == "running" else None),
            "round": progress.get("round") if run["status"] == "running" else None,
            "current_tool": progress.get("current_tool") if run["status"] == "running" else None,
            "max_rounds": summary.get("max_rounds"),
            "ran_out_of_rounds": bool(summary.get("rounds_exhausted")),
            "result_excerpt": summary.get("result_excerpt"),
            "error": summary.get("error"),
        }
        rows.append({key: value for key, value in row.items() if value not in (None, "")})
    return rows


def _attach_stored_results(rows: List[Dict[str, Any]], session_id: Optional[str],
                           owner: Optional[str]) -> int:
    """Give each finished worker row a ``result_ref`` to its whole stored
    result. Returns how many rows got one."""
    attached = tried = 0
    for row in rows:
        if attached >= _MAX_STORED_RESULTS_PER_CALL or tried >= 2 * _MAX_STORED_RESULTS_PER_CALL:
            break
        if row.get("status") not in _FINISHED_STATUSES or not row.get("worker_session"):
            continue
        tried += 1
        try:
            stored = stored_worker_result(row["run_id"], owner=owner, caller_session=session_id)
        except Exception:
            logger.debug("status: could not store the result of %s", row["run_id"], exc_info=True)
            stored = None
        if not stored:
            continue
        from src.tool_output_store import recall_call

        row["result_ref"] = stored["ref"]
        row["result_chars"] = stored["chars"]
        row["read_result"] = recall_call(stored["ref"])
        attached += 1
    return attached


async def _status(args: Dict[str, Any], session_id: Optional[str],
                  owner: Optional[str] = None) -> Dict[str, Any]:
    """``status`` (alias ``poll``/``wait``/``check``), optionally for one run
    and optionally waiting, bounded, until a running worker finishes."""
    run_id = str(args.get("run_id") or "").strip()
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    loadout = str(args.get("name") or "").strip()
    rows = _status_rows(session_id, limit, run_id, loadout)
    if run_id and not rows:
        known = [row["run_id"] for row in _status_rows(session_id, limit, "", loadout)]
        return {"error": (f"status: no worker run {run_id!r}"
                          + (f" of loadout {loadout!r}" if loadout else "")
                          + " was started by this chat. "
                          + (f"Its runs: {', '.join(known[:10])}." if known else "It has started none.")),
                "exit_code": 1}
    if loadout and not rows:
        return {"response": (f"This chat has started no worker runs with loadout {loadout!r}. "
                             "Omit name to see every worker this chat started."),
                "runs": [], "running": 0, "loadout": loadout, "exit_code": 0,
                "progress_key": "[]"}

    waited = 0.0
    running_ids = [row["run_id"] for row in rows if row.get("status") == "running"]
    if running_ids:
        key = f"{session_id}|{run_id or '*'}"
        wait = _status_wait(key, _requested_wait(args))
        if wait > 0:
            started = time.monotonic()
            deadline = started + wait
            from src import agent_activity

            while time.monotonic() < deadline:
                await asyncio.sleep(min(_STATUS_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                if any((agent_activity.get_run(rid) or {}).get("status", "running") != "running"
                       for rid in running_ids):
                    break
            waited = round(time.monotonic() - started, 1)
            rows = _status_rows(session_id, limit, run_id, loadout)
    running = sum(1 for row in rows if row.get("status") == "running")
    if not running:
        _last_status_checks.pop(f"{session_id}|{run_id or '*'}", None)
    cut_off = [row["run_id"] for row in rows if row.get("ran_out_of_rounds")]
    stored = _attach_stored_results(rows, session_id, owner)
    response = (
        f"{len(rows)} worker run(s) for this chat"
        + (f" with loadout {loadout!r}" if loadout else "")
        + f"; {running} still running."
        + (f" Cut off before finishing: {', '.join(cut_off)} — these did NOT finish their task; "
           "restart them with a narrower task rather than reporting their partial work as done."
           if cut_off else "")
        + " A result_excerpt is the worker's own claim, not verified work."
    )
    if stored:
        response += (
            " Each finished worker's WHOLE result is stored: read it with recall_tool_output and the "
            "row's result_ref (read_result is the exact call). Do not send_to_session the worker to "
            "repeat its result — it would rewrite it from memory, without its tools."
        )
    if running:
        response += (
            f" Still running{f' after waiting {waited:g}s' if waited else ''}. Its result is handed back to "
            "this chat automatically when it finishes, so do not keep checking: end your turn and tell the "
            f"user, or call status again with wait_seconds (up to {MAX_STATUS_WAIT_S}) to block until it is done."
        )
    out: Dict[str, Any] = {"response": response, "runs": rows, "running": running, "exit_code": 0}
    if loadout:
        out["loadout"] = loadout
    if waited:
        out["waited_seconds"] = waited
    # The loop's stall detector reads this instead of the whole result, so a
    # re-check that shows only a later timestamp is not taken for progress.
    out["progress_key"] = json.dumps(
        [[row["run_id"], row.get("status"), row.get("tool_calls"), row.get("current_tool")] for row in rows],
        default=str)
    return out


def _import_prepare(policy: Dict[str, Any], *, overwrites: bool = True,
                    session_id: Optional[str] = None, owner: Optional[str] = None):
    """The ``create`` rule for each imported profile, as a transfer ``prepare``.

    An imported file is agent-supplied input like any other: the same
    tool-policy refusal, clamp to this chat's policy and starved-loadout check
    apply, so a file cannot carry in a wider loadout than ``create`` would store.
    A profile that would overwrite a saved one of the same name is held to the
    ``update`` rule too: it may not widen it without the user.
    """
    def prepare(profile: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        requested = dict(profile)
        error, scope_notes = _scope_tools(requested, policy, action="import",
                                          supplied_access=str(profile.get("tool_access") or ""))
        if error:
            raise ValueError(error["error"])
        clamped, narrowed = agent_loadouts.clamp(requested, policy)
        narrowed = [*scope_notes, *narrowed]
        if agent_loadouts.tool_starved(narrowed):
            raise ValueError("none of its tools are available to this chat: " + "; ".join(narrowed))
        existing = agent_profiles.get_profile(clamped["name"]) if overwrites else None
        if existing is not None:
            widened = agent_loadouts.widenings(existing, clamped)
            if widened["notes"] and not _user_authorized_widening(
                    session_id, owner, existing["name"], widened["tools"]):
                raise ValueError(
                    f"would widen the saved loadout {existing['name']!r} ({'; '.join(widened['notes'])}) "
                    "without the user asking for it; import with rename_conflicts=true to keep both, or "
                    "ask the user first")
        return clamped, narrowed
    return prepare


async def manage_agent_loadout(content: str, session_id: Optional[str] = None,
                               owner: Optional[str] = None) -> Dict[str, Any]:
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "manage_agent_loadout: JSON object required", "exit_code": 1}
    if not isinstance(args, dict):
        return {"error": "manage_agent_loadout: JSON object required", "exit_code": 1}

    action = str(args.get("action") or "list").strip().lower()
    action = _ACTION_ALIASES.get(action, action)
    if action not in _ACTIONS:
        return {"error": f"action must be one of {', '.join(_ACTIONS)}", "exit_code": 1}

    if action == "list":
        rows = [agent_loadouts.discovery_summary(p) for p in agent_profiles.load_profiles()]
        return {
            "response": (
                f"{len(rows)} loadout(s). Use action='get' with a name to inspect its full policy "
                "and instructions."
            ),
            "loadouts": rows,
            "exit_code": 0,
        }

    if action == "status":
        # What happened to the workers this chat started. Without it the only
        # way to find out was to grep the activity JSONL by hand -- which is
        # exactly what the 2026-09-17 transcript spent twenty rounds doing,
        # while the answer sat in the run registry the whole time.
        return await _status(args, session_id, owner)

    # Read after the two read-only actions, which grant nothing and so need
    # no policy: a status poll no longer costs a policy read (a DB query plus
    # the MCP inventory), and cannot fail on one.
    policy = agent_loadouts.caller_policy(session_id, owner)

    if action == "stop":
        # Actually stop a worker this chat started. Without it, "stop the
        # scout" reached the worker only as a message (message_agent), which
        # it read and ignored for ten more rounds until the user cancelled it
        # by hand. Same path as the Agents panel's Stop button.
        from src import agent_activity
        from src import agent_control

        run_id = str(args.get("run_id") or "").strip()
        worker = str(args.get("worker_session") or args.get("session_id") or "").strip()
        if worker:
            from src.ai_interaction import get_session_manager
            from src.agent_tools.session_tools import resolve_session_ref

            manager = get_session_manager()
            if manager is not None:
                worker = resolve_session_ref(manager, worker, owner=owner)
        mine = agent_activity.list_runs(session_id=session_id, limit=100, include_descendants=True)
        candidates = [
            run for run in mine
            if run.get("status") == "running"
            and (not run_id or run.get("run_id") == run_id)
            and (not worker or (run.get("summary") or {}).get("target_session") == worker
                 or run.get("session_id") == worker)
        ]
        if not run_id and not worker:
            if len(candidates) != 1:
                running = [{"run_id": r["run_id"], "title": r.get("title")} for r in candidates]
                return {"error": "stop needs run_id (see action=status); "
                                 f"{len(running)} worker(s) running: {running}", "exit_code": 1}
        if not candidates:
            return {"error": "No running worker started by this chat matches; see action=status.", "exit_code": 1}
        stopped = []
        for run in candidates:
            try:
                result = await agent_control.stop_run(
                    run["run_id"], by="by the chat that started it (manage_agent_loadout stop)")
            except (LookupError, ValueError) as exc:
                result = {"stopped": False, "reason": str(exc)}
            stopped.append({"run_id": run["run_id"], "title": run.get("title"), **result})
        ok = any(item.get("stopped") for item in stopped)
        return {"response": ("Stopped: " if ok else "Not stopped: ") + ", ".join(
                    f"{item['run_id']} ({item.get('how') or item.get('reason') or item.get('status')})" for item in stopped),
                "runs": stopped, "exit_code": 0 if ok else 1}

    if action == "capabilities":
        # What a loadout authored from this chat may contain at most. Without
        # it the agent has to discover the clamp by tripping over it.
        detail = bool(args.get("detail"))
        ceiling = {
            # The tool schema already tells an agent callable names. Repeating
            # every permitted name here costs a large tool result before it has
            # picked a loadout; request detail=true only when authoring an
            # explicit selected-tools policy.
            "tool_count": len(policy["allowed_tools"]),
            "tool_examples": sorted(policy["allowed_tools"])[:12],
            "memory_access": policy["memory_access"],
            "skill_access": policy["skill_access"],
            "skills": sorted(policy["skill_names"]),
            "model_access": policy["model_access"],
            "allowed_models": sorted(policy["allowed_models"]),
            "allowed_mcp_servers": list(policy["allowed_mcp_servers"]),
            # `tool_examples`/`tools` list only the native names, because MCP
            # tool names are generated at runtime. Say how to grant them rather
            # than leaving the agent to find out by being refused.
            "mcp_tools": (
                "enabled_tools also takes mcp__<server>__<tool> for one tool, "
                "mcp__<server>__* for a whole server and mcp__* for all of them, "
                "limited to allowed_mcp_servers above"
            ),
            "private_vault_access": policy["private_vault_access"],
            "shell_access": policy.get("shell_access") or "sandbox",
            "delegation_policy": policy["delegation_policy"],
            "max_parallel_workers": policy["max_parallel_workers"],
            "worker_limit_scope": "parent_chat (separate from provider-wide concurrent jobs)",
            "approval_mode_floor": policy["approval_mode"],
        }
        if detail:
            ceiling["tools"] = sorted(policy["allowed_tools"])
        return {
            "response": "Ceiling for loadouts created from this chat",
            "ceiling": ceiling,
            "exit_code": 0,
        }

    if action == "export":
        from src import agent_profile_transfer

        try:
            document = agent_profile_transfer.export_profiles(args.get("names"))
        except ValueError as exc:
            return {"error": f"export: {exc}", "exit_code": 1}
        return {"response": f"Exported {len(document['profiles'])} loadout(s)", "document": document,
                "exit_code": 0}

    if action == "import":
        from src import agent_profile_transfer

        document = args.get("document")
        if isinstance(document, str):
            try:
                document = json.loads(document)
            except ValueError:
                return {"error": "import: document is not valid JSON", "exit_code": 1}
        try:
            report = agent_profile_transfer.import_profiles(
                document, mode=str(args.get("mode") or "merge"),
                rename_conflicts=bool(args.get("rename_conflicts")),
                prepare=_import_prepare(policy,
                                        overwrites=(str(args.get("mode") or "merge").strip().lower() == "replace"
                                                    or not bool(args.get("rename_conflicts"))),
                                        session_id=session_id, owner=owner),
                check_model=lambda spec: _model_problem(spec, owner),
            )
        except ValueError as exc:
            return {"error": str(exc), "exit_code": 1}
        return {"response": agent_profile_transfer.summary_line(report), "report": report,
                "exit_code": 0 if report["written"] or not report["errors"] else 1}

    name = str(args.get("name") or (args.get("loadout") or {}).get("name") or "").strip()

    if action == "get":
        profile = agent_profiles.get_profile(name)
        if profile is None:
            return {"error": f"no loadout named {name!r}", "exit_code": 1}
        return {"response": f"Loadout {profile['name']}", "loadout": agent_loadouts.summarize(profile),
                "instructions": profile["instructions"], "exit_code": 0}

    if action == "preflight":
        profile = agent_profiles.get_profile(name)
        if profile is None:
            return {"error": f"no loadout named {name!r}", "exit_code": 1}
        from src.profile_readiness import profile_readiness, render

        readiness = profile_readiness(profile, policy, owner,
                                      required_tools=args.get("required_tools") or [],
                                      inherited_model=_caller_model(session_id) if profile.get("allowed_models")
                                      else None)
        return {"response": f"Loadout {profile['name']}: {render(readiness)}",
                "readiness": readiness, "exit_code": 0}

    if action == "delete":
        if not agent_loadouts.delete(name):
            return {"error": f"no loadout named {name!r}", "exit_code": 1}
        return {"response": f"Deleted loadout {name!r}", "exit_code": 0}

    if action in ("create", "update"):
        base = None
        if action == "update":
            base = agent_profiles.get_profile(name)
            if base is None:
                return {"error": f"no loadout named {name!r}; use action='create'", "exit_code": 1}
        requested = _requested(args, base)
        if not requested.get("name"):
            return {"error": "name is required", "exit_code": 1}
        # Keep the stored name's capitalisation rather than whatever the caller
        # typed, so `update` never renames a loadout as a side effect.
        if base is not None:
            requested["name"] = base["name"]
        supplied = _supplied(args)
        error, scope_notes = _scope_tools(
            requested, policy, action=action,
            supplied_access=str(supplied.get("tool_access") or "").strip().lower())
        if error:
            return error
        # Only the models named in THIS call are checked, so an endpoint that is
        # down does not block renaming a loadout that already has a model.
        if policy["model_access"] != "current":
            for spec in [supplied.get("model"), *(supplied.get("model_fallbacks") or [])]:
                problem = _model_problem(str(spec), owner) if spec else None
                if problem:
                    available = _available_model_ids(owner)
                    return {
                        "error": (
                            f"{action}: model {spec!r} is not available: {problem}. "
                            + (f"Available model ids: {', '.join(available)}. "
                               if available else "")
                            + "Use one of those exactly, or omit model to inherit the calling chat's."
                        ),
                        "available_models": available,
                        "exit_code": 1,
                    }
        requested_tools = list(requested.get("enabled_tools") or [])
        required_tools = args.get("required_tools") or (args.get("loadout") or {}).get("required_tools") or []
        if not isinstance(required_tools, list):
            return {"error": "required_tools must be a list of exact tool names", "exit_code": 1}
        if required_tools and requested.get("tool_access") == "selected":
            requested["enabled_tools"] = sorted(set(requested_tools) | {str(t) for t in required_tools})
            requested_tools = requested["enabled_tools"]
        # A widening the call asked for, not a narrowing: reported apart from
        # `narrowed`, and never mistaken for a downgrade of untouched fields.
        released = _release_stale_denials(requested, base, supplied, required_tools) if base is not None else []
        try:
            profile, narrowed = agent_loadouts.clamp(requested, policy)
        except ValueError as exc:
            return {"error": f"manage_agent_loadout: {exc}", "exit_code": 1}
        narrowed = [*scope_notes, *narrowed]
        if action == "update":
            unrequested = _unrequested_narrowings(narrowed, supplied)
            if unrequested:
                # Refuse rather than store a downgrade nobody asked for. This
                # chat cannot keep those settings (it may not grant more than
                # it has), and it must not quietly lower them either.
                return {
                    "error": (
                        f"update: {requested['name']!r} was not saved. This loadout has settings wider than "
                        "this chat may grant, and saving it from here would lower them although this call "
                        "did not change them: " + "; ".join(unrequested) + ". Ask the user to make this edit "
                        "in Settings > Agent loadouts, or change only fields this chat can grant. Do not "
                        "start another agent to retry it: a worker's limits are never wider than its parent's."
                    ),
                    "blocked": True,
                    "blocked_reason": "update_would_downgrade_untouched_fields",
                    "would_narrow": unrequested,
                    "exit_code": 1,
                }
            widened = agent_loadouts.widenings(base, profile)
            if widened["notes"]:
                auth = _widening_authorization(session_id, owner, base["name"], widened["tools"])
                if not auth.ok:
                    refusal = _refuse_widening(session_id, base["name"], widened, auth)
                    logger.info("[agent-loadout] refused widening update of %s from %s%s: %s",
                                base["name"], session_id, " (repeat)" if refusal.get("repeat") else "",
                                "; ".join(widened["notes"]))
                    return refusal
                logger.info("[agent-loadout] widening update of %s from %s authorised by the user's "
                            "message: %s", base["name"], session_id, "; ".join(widened["notes"]))
        matrix = agent_loadouts.capability_matrix(requested_tools, required_tools, profile, policy, owner)
        if matrix["mission_critical_missing"]:
            # A profile that saves without what its mission needs is the
            # failure this refuses: it only moves the error to every worker
            # it would ever start.
            return {
                "error": (
                    f"{action}: {requested['name']!r} was not saved; required tool(s) unavailable: "
                    + "; ".join(f"{row['tool']} ({row['reason']}: {row['detail']})"
                                for row in matrix["denied"] if row["tool"] in matrix["mission_critical_missing"])
                    + ". Fix the cause (reconnect the server, widen this chat's policy) or drop the "
                    "requirement."
                ),
                "capabilities": matrix,
                "narrowed": narrowed,
                "exit_code": 1,
            }
        if agent_loadouts.tool_starved(narrowed):
            # Storing it would only defer the failure to every worker it ever
            # starts. Refuse here, where the author can still fix it.
            return {
                "error": (
                    f"{action}: none of the tools requested for {requested['name']!r} are available to this "
                    "chat, so the loadout would start workers with no tools at all. "
                    + "; ".join(narrowed)
                    + f". This chat can grant: {_tool_examples(policy)}. "
                    "Pick from those, or ask the user to widen this chat's own tool access."
                ),
                "narrowed": narrowed,
                "available_tool_count": len(policy["allowed_tools"]),
                "exit_code": 1,
            }
        try:
            saved = agent_loadouts.save(profile, replace=(action == "update"))
        except ValueError as exc:
            return {"error": f"manage_agent_loadout: {exc}", "exit_code": 1}
        response = f"{'Updated' if action == 'update' else 'Created'} loadout {saved['name']!r}"
        if narrowed:
            response += f"; narrowed to this chat's own policy in {len(narrowed)} place(s)"
        if released:
            response += "; " + "; ".join(note.split(": ", 1)[-1] for note in released)
        try:
            from src.profile_readiness import profile_readiness, render

            # Judged against what this call ASKED for, not only what was kept:
            # a requested tool the save dropped is a failed check, so the
            # readiness line cannot say READY for a loadout missing it.
            readiness = profile_readiness(saved, policy, owner, required_tools=required_tools,
                                          requested_tools=requested_tools,
                                          inherited_model=_caller_model(session_id) if saved.get("allowed_models")
                                          else None)
            response += ". Readiness " + render(readiness)
        except Exception as exc:
            logger.warning("loadout: readiness check failed for %s", saved["name"], exc_info=True)
            readiness = {"status": "UNKNOWN", "error": f"{type(exc).__name__}"}
            response += f". Capability status: {matrix['status']} (readiness check failed)"
        readback = agent_profiles.get_profile(saved["name"])
        consistent = bool(readback) and (
            set(readback.get("enabled_tools") or []) == set(saved.get("enabled_tools") or [])
            and readback.get("tool_access") == saved.get("tool_access")
            and set(readback.get("skill_names") or []) == set(saved.get("skill_names") or []))
        if not consistent:
            response += ". WARNING: the stored loadout read back with different bindings"
        return {"response": response, "loadout": agent_loadouts.summarize(saved),
                "capabilities": matrix, "readiness": readiness, "readback_consistent": consistent,
                "narrowed": narrowed, **({"granted": released} if released else {}), "exit_code": 0}

    # action == "start"
    task = str(args.get("task") or "").strip()
    if not task:
        # Spell out the call shape: two rounds were spent on
        # {"action":"start","name":...,"detail":true} before the model found
        # that `task` is the whole assignment and `detail` is not a start field.
        return {
            "error": (
                "start: 'task' is required and was not given. Send "
                '{"action": "start", "name": "<loadout>", "task": "<the whole assignment>"} — '
                "the worker begins with no other context. 'detail' is a capabilities parameter, "
                "not a start one."
            ),
            "exit_code": 1,
        }
    if policy["delegation_policy"] == "never":
        return {"error": "start: this chat's delegation policy is 'never'", "exit_code": 1}
    from src import agent_control

    # Same limit the other spawning tools are held to (the gate in
    # tool_execution). Checked here because that gate keys on the tool name,
    # and this tool's read-only actions must not be blocked by a busy chat.
    limit = policy["max_parallel_workers"]
    running = agent_control.live_children(session_id)
    if limit <= 0 or running >= limit:
        return {
            "error": (
                f"Worker capacity reached: {running} active of this chat's limit {limit}. "
                "This is the parent chat's Child workers limit, separate from provider-wide concurrent jobs. "
                "Do not retry a start while capacity is unchanged."
            ),
            "blocked": True,
            "blocked_reason": "worker_capacity",
            "capacity_scope": "parent_chat",
            "configuration_hint": "Agents > select the parent chat > Loadout > Child workers. Only the user may raise this ceiling.",
            "capacity": {"limit": limit, "active": running, "available": max(0, limit - running)},
            "exit_code": 1,
        }
    started_profile = agent_profiles.get_profile(name) if name else None
    if name and started_profile is None:
        rows = agent_profiles.load_profiles()
        available = ", ".join(
            f"{p['name']} ({len(p['enabled_tools'])} tools)" if p["tool_access"] == "selected"
            else f"{p['name']} ({p['tool_access']} tools)"
            for p in rows
        ) or "none are defined"
        return {
            "error": (
                f"no loadout named {name!r}. Available: {available}. "
                "Pick one whose tools actually fit this task, or create one first — "
                "a near-miss name is not a near-miss loadout."
            ),
            "exit_code": 1,
        }
    # A worker starting its own loadout again is handing its task to a copy of
    # itself one level deeper, where it has the same tools or fewer. On
    # 2026-09-24 an Odysseus Admin worker whose loadout edit was clamped did
    # exactly that to "retry with a wider ceiling"; the copy sat at the depth
    # limit and could do nothing. Workers only: a person's chat running under a
    # loadout may still start another one of it to work in parallel.
    if started_profile is not None and session_id:
        try:
            from core.database import get_session_settings

            _caller = get_session_settings(session_id) or {}
        except Exception:
            _caller = {}
        if (_caller.get("parent_session")
                and str(_caller.get("agent_profile") or "").casefold() == started_profile["name"].casefold()):
            return {
                "error": (
                    f"start: this worker already runs as {started_profile['name']!r}; starting another copy "
                    "gives the task to an agent with the same limits or fewer. Do the work yourself, or "
                    "report what blocks you to the chat that started you."
                ),
                "blocked": True,
                "blocked_reason": "worker_started_its_own_loadout",
                "exit_code": 1,
            }

    # The same loadout already working in another chat (a person's chat
    # running it, or a worker another chat started). On 2026-09-30 the admin
    # agent, told about "the implement feature and open pr engineer", started
    # a second Lead Engineer instead of looking at the one already 40 minutes
    # into its task. Its own workers do not count: those are parallel work
    # this chat chose. `parallel: true` starts it anyway.
    if started_profile is not None and not args.get("parallel"):
        from src.chat_work import describe_rows, running_chats

        busy = [r for r in running_chats(owner)
                if str(r.get("profile") or "").casefold() == started_profile["name"].casefold()
                and r.get("session_id") != session_id and r.get("parent_session") != session_id]
        if busy:
            return {
                "error": (
                    f"start: {started_profile['name']} is already working in another chat. If the user means "
                    "that agent, check on it (manage_session running), tell it something (message_agent) or "
                    "stop it (manage_session stop) instead of starting a copy. To start another one for "
                    "separate work, call start again with parallel: true.\n" + describe_rows(busy[:5])
                ),
                "blocked": True,
                "blocked_reason": "same_loadout_running",
                "running": busy[:5],
                "exit_code": 1,
            }

    # One-off tools for this run only. A parent whose worker lacks a tool for
    # the task at hand used to widen the SAVED loadout to get past it
    # (2026-09-28: "Memory Data Structure Innovator" went from [web_search] to
    # eleven tools, for good, without the user). `extra_tools` gives this one
    # worker the extra tools, clamped to what this chat may grant exactly as
    # create is, and leaves the loadout as the user saved it.
    extra_raw = args.get("extra_tools")
    if isinstance(extra_raw, str):
        extra_raw = [extra_raw]
    if extra_raw is not None and not isinstance(extra_raw, list):
        return {"error": "start: extra_tools must be a list of exact tool names", "exit_code": 1}
    extra = sorted({str(t).strip() for t in (extra_raw or []) if str(t).strip()})
    one_off: Optional[Dict[str, Any]] = None
    extra_note = ""
    extra_report: Dict[str, Any] = {}
    if extra:
        if started_profile is None:
            return {"error": "start: extra_tools adds tools to a named loadout for one run; pass name too",
                    "exit_code": 1}
        one_off, granted_extra, refused_extra = _one_off_profile(started_profile, extra, policy)
        if refused_extra and not granted_extra:
            return {
                "error": (
                    f"start: none of extra_tools ({', '.join(refused_extra)}) can be granted from this chat: "
                    f"it may not use them itself. This chat can grant: {_tool_examples(policy)}."
                ),
                "blocked": True,
                "blocked_reason": "extra_tools_not_grantable",
                "exit_code": 1,
            }
        extra_report = {"granted": granted_extra, **({"refused": refused_extra} if refused_extra else {})}
        if one_off is None:
            extra_note = (f" extra_tools ignored: loadout {started_profile['name']!r} already grants every tool "
                          "this chat's workers may use.")
        else:
            extra_note = (f" For this run only it also has {', '.join(granted_extra)} (extra_tools); the saved "
                          "loadout is unchanged."
                          + (f" Not granted (this chat may not use them): {', '.join(refused_extra)}."
                             if refused_extra else ""))
    effective = one_off or started_profile

    # A worker that cannot read, search or run anything will spend its whole
    # round budget explaining that. Refuse at launch rather than produce one.
    unusable = agent_loadouts.unusable_reason(effective) if effective else None
    if unusable:
        usable = [p["name"] for p in agent_profiles.load_profiles()
                  if agent_loadouts.unusable_reason(p) is None]
        return {
            "error": (
                f"start: {unusable}. Start it with extra_tools=[...] naming the tools this task needs "
                "(for this run only), ask the user to give the loadout tools, or start one of: "
                + (", ".join(usable) if usable else "none — every stored loadout has this problem")
            ),
            "blocked": True,
            "blocked_reason": "loadout_has_no_tools",
            "loadout": agent_loadouts.summarize(started_profile),
            "exit_code": 1,
        }

    # A loadout that names MCP tools its server no longer offers starts a
    # worker without them, and the worker can only report that its job is
    # impossible. When every named tool on a connected server is gone, that
    # server's part of the role is gone too: refuse and say which names.
    stale = agent_loadouts.stale_mcp_grants(effective) if effective else {}
    dead_servers = {s: b["missing"] for s, b in stale.items() if not b["present"]}
    if dead_servers:
        listed = "; ".join(f"server {s}: {', '.join(names)}" for s, names in sorted(dead_servers.items()))
        return {
            "error": (
                f"start: loadout {name!r} names MCP tools its connected server does not offer "
                f"({listed}). The server is connected but its tools have different names now, so "
                "the worker would start with none of them. Update the loadout's enabled_tools "
                "(action='capabilities' lists what exists, or grant the server whole with "
                "mcp__<server>__*), then start again."
            ),
            "blocked": True,
            "blocked_reason": "loadout_mcp_tools_missing",
            "missing_mcp_tools": dead_servers,
            "exit_code": 1,
        }
    stale_note = ""
    if stale:
        stale_note = " Note: these granted MCP tools do not exist on their server and will not be available: " + \
            ", ".join(sorted(n for b in stale.values() for n in b["missing"])) + "."

    # Say before the worker runs what it will not have, rather than letting it
    # discover that mid-task and hand back "blocked" (2026-09-26: a repository
    # task started on a loadout with no git tool).
    withheld = (agent_loadouts.worker_withheld_tools(effective, policy.get("worker_depth", 0), policy)
                if effective else {})
    missing_repo: List[str] = []
    document_warning: Optional[str] = None
    if effective is not None:
        from src.worker_preflight import document_access_warning, task_needs_workspace

        if task_needs_workspace(task):
            missing_repo = agent_loadouts.missing_repository_tools(effective)
        document_warning = document_access_warning(task, effective)
    gap_note = ""
    if withheld:
        gap_note += (" This worker will NOT get " + ", ".join(sorted(withheld))
                     + " although the loadout lists " + ("them" if len(withheld) > 1 else "it") + ": "
                     + "; ".join(sorted(set(withheld.values()))) + ".")
    if missing_repo:
        gap_note += (f" The task is repository work but loadout {started_profile['name']!r} does not grant "
                     f"{' or '.join(missing_repo)}, so the worker cannot see git status, diffs or history"
                     + (" or commit on a branch" if "manage_agent_worktree" in missing_repo else "")
                     + ". If the task needs that, stop it and start it again with extra_tools="
                     + json.dumps(missing_repo) + " (this run only; adding them to the saved loadout "
                     "needs the user).")
    if document_warning:
        gap_note += " Warning: " + document_warning

    # A worker always reports to the chat that started it. `parent_session`
    # used to accept "" (standalone) or any chat the user owns, and a model
    # that filled the optional field -- empty, or with an id it had seen
    # earlier -- orphaned the worker: no card in the chat that started it, no
    # row in action='status', and no hand-off when it finished (2026-09-23).
    # Another chat is accepted only when it is one of this chat's own workers.
    requested = str(args.get("parent_session") or "").strip()
    parent = session_id or None
    if requested and requested != session_id:
        from src import agent_activity as _activity
        if session_id and requested in _activity._descendant_sessions(
                _activity.list_runs(owner=owner, limit=400), session_id):
            parent = requested
        else:
            logger.info("[agent-loadout] ignoring parent_session=%s; reporting to calling chat %s",
                        requested, session_id)
    if parent and parent != session_id:
        # /api/agents/launch owner-checks its parent chat; this path has to do
        # the same, or an agent could name someone else's chat and have the
        # worker's model copied from it and its progress published into it.
        # Exact owner match, as in send_to_session: a null-owner session is not
        # an authenticated caller's either.
        error = {"error": f"parent_session {parent!r} not found", "exit_code": 1}
        try:
            from src.ai_interaction import get_session_manager

            manager = get_session_manager()
            target = manager.get_session(parent) if manager else None
        except Exception:
            return error
        if target is None or (owner and getattr(target, "owner", None) != owner):
            return error
    # The model the worker will run on has to be one its loadout allows.
    # 2026-09-28: a loadout with allowed_models=['gpt-5.6-sol'] and no model of
    # its own started a worker on the parent chat's gpt-6-sol.
    start_model = str(args.get("model") or "").strip()
    if effective is not None and effective.get("allowed_models"):
        # A worker of a loadout naming no model gets the model of the chat it reports to.
        inherited = "" if (start_model or effective.get("model")) else _caller_model(parent)
        problem = agent_loadouts.model_problem(effective, start_model=start_model or None,
                                               inherited_model=inherited or None)
        if problem and not start_model:
            # Only the inherited chat model is outside the list: run the
            # loadout's first allowed model rather than refuse a start the
            # caller cannot fix without guessing (the repair is always "pass
            # model=allowed[0]", so do that).
            start_model = problem["allowed_models"][0]
            logger.info("[agent-loadout] %s: the calling chat's model %r is not in allowed_models; "
                        "starting on %r", name, problem["model"], start_model)
            problem = None
        if problem:
            return {
                "error": f"start: loadout {name!r}: {problem['detail']}. Repair: {problem['repair']}.",
                "blocked": True,
                "blocked_reason": "model_not_allowed",
                "model": problem["model"],
                "allowed_models": problem["allowed_models"],
                "next_action": {"retry_with": {"model": problem["allowed_models"][0]}},
                "exit_code": 1,
            }

    requires = args.get("requires") or []
    if isinstance(requires, str):
        requires = [requires]
    launch: Dict[str, Any] = {
        "owner": owner, "task": task, "parent_session": parent,
        "workspace": str(args.get("workspace") or "").strip() or None,
        "requires": [str(r) for r in requires] if isinstance(requires, list) else [],
    }
    if one_off is not None:
        # The saved loadout plus this run's extra tools, carried as the
        # worker's own policy; launch_worker persists it on the worker chat.
        launch["inline_profile"] = {**one_off, **({"model": start_model} if start_model else {})}
    else:
        launch.update(profile_name=name or None, model=start_model or None)
    try:
        result = await agent_control.launch_worker(**launch)
    except WorkerBlocked as exc:
        payload = dict(exc.payload)
        needed = list((payload.get("next_action") or {}).get("enable_tools") or [])
        if started_profile is not None and needed and payload.get("code") in ("TOOLS_UNAVAILABLE",
                                                                              "WRITE_NOT_ALLOWED"):
            wanted = sorted(set(extra) | set(needed))
            payload["hint"] = (
                f"If this task really needs {', '.join(needed)}, start {started_profile['name']!r} again with "
                f"extra_tools={json.dumps(wanted)} — for this run only. Do not widen the saved loadout "
                "for one task: that needs the user.")
            payload["suggested_start"] = {"action": "start", "name": started_profile["name"],
                                          "extra_tools": wanted}
        return payload
    except (ValueError, RuntimeError) as exc:
        return {"error": f"start: {exc}", "exit_code": 1}
    # What it is actually going to run with. The model has to be able to see a
    # wrong-fit loadout without waiting for the worker to report that it could
    # not do the job, and the round budget is the number an "it ran out of
    # rounds" result has to be read against.
    granted = (effective["enabled_tools"] if effective
               and effective["tool_access"] == "selected" else
               (effective["tool_access"] if effective else "all"))
    # The full inventory stays out of both the log and the model's context: a
    # preview and a count say what was granted; `get` has the whole list.
    preflight = {
        "loadout": started_profile["name"] if started_profile else "ad-hoc worker",
        "model": result.get("model") or "inherit",
        "max_rounds": result.get("max_rounds"),
        "tools": granted if isinstance(granted, str) else list(granted[:_TOOL_LIST_PREVIEW]),
        "tool_count": None if isinstance(granted, str) else len(granted),
        "skills": started_profile["skill_names"] if started_profile else [],
        "allowed_mcp_servers": started_profile["allowed_mcp_servers"] if started_profile else [],
        **({"withheld_from_worker": withheld} if withheld else {}),
        **({"missing_repository_tools": missing_repo} if missing_repo else {}),
        **({"extra_tools": extra_report} if extra_report else {}),
        **({"document_access_warning": document_warning} if document_warning else {}),
        **(result.get("preflight") or {}),
    }
    tool_note = _tool_list_note(granted)
    logger.info("[agent-loadout] start loadout=%s run=%s child=%s model=%s rounds=%s tools=%s%s",
                preflight["loadout"], result.get("run_id"), result.get("session_id"),
                preflight["model"], preflight["max_rounds"], tool_note,
                f" extra={','.join(extra_report.get('granted') or [])}" if one_off is not None else "")
    try:
        # The wrap-up round is asked for on a saved loadout's own run; a
        # one-off run (extra_tools) starts from an inline copy, where the
        # budget stays advisory.
        wrap_up = int(result.get("max_rounds") or 0) if name and one_off is None else 0
    except (TypeError, ValueError):
        wrap_up = 0
    wrap_note = (f"; at round {wrap_up} it is asked to wrap up and hand back what it has, "
                 "including what is left" if wrap_up > 0 else "")
    return {
        "response": (
            f"Started {name or 'worker'} in chat {result.get('session_name')} on {preflight['model']} "
            f"with these tools: {tool_note}. It runs until the task is done — a round count never "
            f"cuts it off{wrap_note} — and it runs detached, so its progress appears on this chat's activity feed "
            "and in action='status'. If those tools cannot do the task you just described, stop it "
            "and fix the loadout instead of waiting for the result." + extra_note + stale_note + gap_note
            + " Its result is handed back to this chat automatically when it finishes, so there is no need "
            "to poll: end your turn, or use action='status' with wait_seconds to block until it is done."
        ),
        "preflight": preflight,
        **{key: value for key, value in result.items() if key != "preflight"},
        "exit_code": 0,
    }

class ManageAgentLoadoutTool:
    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        return await manage_agent_loadout(content, ctx.get("session_id"), owner=ctx.get("owner"))
