"""Which saved loadout a request looks like work for.

The user should not have to pick the agent. Two signals, both cheap and both
explainable in one line:

* **Capability gap** (strong). Tool retrieval matched MCP tools for this
  request that the current chat's policy denies, and a loadout grants that
  server *specifically* — an exact tool or ``mcp__<server>__*``. That is the
  2026-09-23 Penpot turn exactly: retrieval found ``mcp__c5ec6d7a__create_project``,
  the chat's own loadout dropped it, and Penpot Product Designer is the loadout
  built for that server. A loadout that grants every server (``mcp__*``) or has
  no allowlist at all matches everything, so it is not evidence of fit and is
  not counted.
* **Words** (weaker). A distinctive word of the request appears in the
  loadout's name or persona ("penpot", "designer"), or two appear in its
  description or skill names.

This module only ranks. Whether the chat may act on a match — start the worker
itself, or only suggest it — is the delegation policy's decision, made in the
agent loop.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

_WORD_RE = re.compile(r"[a-z][a-z0-9]+")
_STOPWORDS = frozenset("""
    about after again agent agents also another based because been before being
    below between both build can could create does doing done down each even
    from have having here into just like little make more most much must need
    only other over please quick really should small some something such than
    that their them then there these they thing this those through under used
    using very want what when where which while will with without would your
    test tests work chat create task tasks thing things export
""".split())


_ABOUT_LOADOUTS_RE = re.compile(r"\b(?:loadouts?|presets?|agent\s+profiles?)\b", re.IGNORECASE)


def _words(text: str) -> Set[str]:
    return {w for w in _WORD_RE.findall(str(text or "").lower()) if len(w) >= 4 and w not in _STOPWORDS}


def _hits(query_words: Set[str], target_words: Set[str]) -> Set[str]:
    """Query words found in the target, allowing one to be a prefix of the
    other so "design" meets "designer" (both at least five letters)."""
    out = set()
    for q in query_words:
        for t in target_words:
            if q == t or (min(len(q), len(t)) >= 5 and (q.startswith(t) or t.startswith(q))):
                out.add(q)
                break
    return out


def _specific_grant(profile: Dict[str, Any], tool: str) -> bool:
    """Does this loadout grant ``tool`` by naming it or its server?"""
    from src.tool_policy import split_mcp_tool_name

    parts = split_mcp_tool_name(tool)
    if not parts or profile.get("tool_access") != "selected":
        return False
    entries = {str(e) for e in profile.get("enabled_tools") or ()}
    if tool in entries or f"mcp__{parts[0]}__*" in entries:
        return tool not in set(profile.get("disabled_tools") or ())
    return False


def suggest_loadouts(
    query: str,
    denied_tools: Iterable[str],
    *,
    current_profile: Optional[str] = None,
    profiles: Optional[List[Dict[str, Any]]] = None,
    limit: int = 2,
) -> List[Dict[str, Any]]:
    """Loadouts that fit this request, best first: ``[{name, reason, tools}]``.

    ``denied_tools`` are the tools retrieval matched for the request that this
    chat's policy took away. ``current_profile`` (the loadout this chat already
    runs under) is never suggested back to it.
    """
    if profiles is None:
        from src.agent_profiles import load_profiles

        profiles = load_profiles()
    if not profiles:
        return []
    from src.agent_loadouts import unusable_reason

    if _ABOUT_LOADOUTS_RE.search(str(query or "")):
        # Work ON the loadouts (edit, reconcile, compare them) names them all
        # and is not a request to hand the work to one of them.
        return []
    denied_mcp = sorted({t for t in denied_tools or () if str(t).startswith("mcp__")})
    query_words = _words(query)
    current = str(current_profile or "").casefold()
    ranked = []
    for profile in profiles:
        name = str(profile.get("name") or "")
        if not name or name.casefold() == current or unusable_reason(profile):
            continue
        gap = [t for t in denied_mcp if _specific_grant(profile, t)]
        name_hits = _hits(query_words, _words(f"{name} {profile.get('persona_name') or ''}"))
        desc_hits = _hits(query_words, _words(
            f"{profile.get('description') or ''} {' '.join(profile.get('skill_names') or [])}"))
        if not (gap or name_hits or len(desc_hits) >= 2):
            continue
        reasons = []
        if gap:
            servers = sorted({t.split("__")[1] for t in gap})
            reasons.append(f"it has tools this chat lacks for the request (MCP server {', '.join(servers)})")
        if name_hits or desc_hits:
            words = sorted(name_hits) + sorted(desc_hits - name_hits)
            reasons.append("the request mentions " + ", ".join(words[:5]))
        ranked.append(((len(gap), len(name_hits), len(desc_hits)),
                       {"name": name, "reason": "; ".join(reasons), "tools": gap[:5]}))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return [row[1] for row in ranked[:limit]]


def loadout_named_in(text: str, profiles: Optional[List[Dict[str, Any]]] = None) -> Set[str]:
    """Saved loadout names the user wrote out ("start Penpot Product Designer").

    Naming a saved agent is asking for it, the same way naming a launcher tool
    is (``agent_loop._delegation_tools_named_by_user``).
    """
    if profiles is None:
        from src.agent_profiles import load_profiles

        profiles = load_profiles()
    lowered = str(text or "").lower()
    found = set()
    for profile in profiles or ():
        name = str(profile.get("name") or "").strip()
        if len(name) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", lowered):
            found.add(name)
    return found


def routing_note(suggestions: List[Dict[str, Any]], *, may_launch: bool) -> str:
    """The context note that tells the model about a fitting loadout."""
    if not suggestions:
        return ""
    lines = [f"- {s['name']}: {s['reason']}" for s in suggestions]
    head = "Saved agent loadouts that fit this request:\n" + "\n".join(lines) + "\n"
    if may_launch:
        return head + (
            "If your own tools cannot do the job well, start the best fit with manage_agent_loadout "
            '{"action": "start", "name": "<loadout>", "task": "<the whole assignment>"} instead of '
            "telling the user you lack the tools. Do it yourself when your own tools suffice."
        )
    return head + (
        "This chat only starts agents when the user asks. Do not claim you lack the tools: say which "
        "loadout fits and ask whether to start it (they can reply with its name)."
    )
