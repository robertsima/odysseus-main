"""Keep a chat's tool definitions byte-identical from request to request.

OpenAI's prompt cache reuses a request's prefix only when it matches exactly,
and tool definitions sit ahead of the conversation in it: "Changes tool names,
descriptions, schemas, ordering" invalidate everything after them. Odysseus
picks each round's tools per request (routing, the tool budget, discover_tools,
tool-free rounds), so on 2026-09-27..29 a changed tool list was behind ~7.3M of
the 21M uncached input tokens -- each change re-billed the whole history of a
long chat (90-150k tokens) -- and the first round of a turn was 6% cached.

pi keeps one fixed tool list and prompt for a whole session. OpenAI's
documented equivalent for a harness that must vary what the model may call is
`tool_choice: {"type": "allowed_tools", ...}`: send the same tools every time
and name the callable subset there ("Use allowed_tools to restrict which tools
are callable while keeping the supplied tools list stable"; a tool-free round
sends tool_choice "none" instead of dropping the tools). Measured on gpt-6-luna
by another pi-based harness: removing one tool cached 0 tokens of the next
turn, the allowed_tools form cached 19.5k.

So, per chat, the tools it has been offered so far are *declared*: kept in the
order they were first offered, never dropped for a round (only when policy
disables them), replaced in place only if a tool's schema itself changes, and
persisted so a restart re-sends the same bytes. Each request sends the declared
list and lets the round's own selection be called. The declared list still
grows when a turn needs a tool it never had -- that costs one miss, as before
-- but a round that narrows, a tool-free wrap-up round, or a follow-up turn
that withholds launchers no longer throws the cache away.

A session whose allowed tools are bounded (a worker loadout or chat policy
with an explicit allow-list, see `bounded_fits`) declares that whole set on its
first request instead (`declare(..., full=...)`). On 2026-10-02 a UI-critic
worker that called `discover_tools` for `render_preview` went from 17 to 18
declared tools and the next request was 0% cached on 52k tokens; eight other
rounds that hour had the same shape, ~100k uncached tokens in all. Declaring
the unused definitions costs about 10% of their tokens per round while cached;
one mid-session addition costs a whole uncached prompt, so discovery inside an
allowed set never touches the tools array again.

Only for the ChatGPT/Codex Responses route on GPT-5.6 and later (where the
behaviour was measured); a backend that rejects `tool_choice` is remembered by
llm_core, which then sends only the callable tools, as before.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING = "chatgpt_stable_tools"
# A declared list past this starts over from the round's selection (one miss):
# schemas the model reads on every request still cost context, cached or not.
MAX_DECLARED = 160
_MAX_SESSIONS = 256
# A bounded loadout is declared whole when it is at most this many tools AND its
# definitions are at most this many tokens (chars/4 over the compact schemas).
# Past either, the schemas read on every request cost more than the growth
# misses they prevent, and the session keeps the grow-as-needed behaviour.
BOUNDED_MAX_TOOLS = 64
BOUNDED_MAX_TOKENS = 20000
# An MCP server's tools are declared together when the first one is needed,
# unless the server is this large (then they come as needed).
GROUP_MAX_TOOLS = 24
GROUP_MAX_TOKENS = 8000
_MODEL_RE = re.compile(r"^gpt-(?:5\.(?:[6-9]|[1-9]\d)|(?:[6-9]|[1-9]\d)(?:\.\d+)?)(?:[-.]|$)", re.I)

_lock = threading.Lock()
_DECLARED: "collections.OrderedDict[str, collections.OrderedDict[str, dict]]" = collections.OrderedDict()


def model_supported(model: Optional[str]) -> bool:
    """GPT-5.6 and later (gpt-5.6-sol, gpt-6-luna, gpt-6.1-sol, ...)."""
    return bool(_MODEL_RE.match(str(model or "").strip()))


def enabled() -> bool:
    try:
        from src.settings import get_setting

        value = get_setting(SETTING, True)
    except Exception:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value)


def route_supported(url: Optional[str], model: Optional[str]) -> bool:
    """Whether requests to this route keep a stable declared tool list."""
    if not url or not model_supported(model) or not enabled():
        return False
    try:
        from src.llm_core import _detect_provider

        return _detect_provider(str(url)) == "chatgpt-subscription"
    except Exception:
        return False


def _name(schema: dict) -> str:
    if not isinstance(schema, dict):
        return ""
    fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    return str(fn.get("name") or "")


def _strip_prose(node):
    """``node`` without any `description`/`title`/`examples` text, recursively."""
    if isinstance(node, dict):
        return {k: _strip_prose(v) for k, v in node.items() if k not in ("description", "title", "examples")}
    if isinstance(node, list):
        return [_strip_prose(v) for v in node]
    return node


def _callable_shape(schema: dict) -> str:
    """What a call depends on: the name and the parameter structure, not the prose.

    A declared tool is replaced only when this changes. On 2026-10-02 a chat's
    tools hash moved `e0dd1d7baf(95)` -> `a674d8987f(95)` at round 17 with the
    same tool count: MCP descriptions are built per request from live state
    (`[MCP:server (identity)]` once the identity probe lands, a server's own
    tools/list refresh), so `entry.get(name) != schema` rewrote an entry in
    place and the next request re-billed 68k tokens at 0% cache (tools precede
    input in the prefix). The model reads the first description it was given
    just as well; only a parameter change makes the old definition wrong.
    """
    fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    params = fn.get("parameters") if isinstance(fn, dict) else None
    try:
        return json.dumps(_strip_prose(params), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(params)


def _store_dir() -> str:
    from src import constants

    return os.path.join(constants.DATA_DIR, "prompt_cache", "declared_tools")


def _store_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:120] or "session"
    return os.path.join(_store_dir(), f"{safe}.json")


def _load(session_id: str) -> "collections.OrderedDict[str, dict]":
    entry = _DECLARED.get(session_id)
    if entry is not None:
        _DECLARED.move_to_end(session_id)
        return entry
    entry = collections.OrderedDict()
    try:
        with open(_store_path(session_id), encoding="utf-8") as fh:
            for schema in json.load(fh).get("tools") or []:
                name = _name(schema)
                if name:
                    entry[name] = schema
    except (OSError, ValueError, AttributeError):
        pass
    _DECLARED[session_id] = entry
    while len(_DECLARED) > _MAX_SESSIONS:
        _DECLARED.popitem(last=False)
    return entry


def _save(session_id: str, entry: "collections.OrderedDict[str, dict]") -> None:
    try:
        os.makedirs(_store_dir(), exist_ok=True)
        path = _store_path(session_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tools": list(entry.values())}, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        logger.debug("declared tools for %s not saved", session_id, exc_info=True)


def preview(session_id: Optional[str], names: Iterable[str]) -> set:
    """The names the chat will have declared once ``names`` are offered: what
    the system prompt's tool-dependent sections are built from, so they change
    only when the declared list does."""
    wanted = {n for n in names or () if n}
    if not session_id:
        return wanted
    with _lock:
        declared = set(_load(session_id))
    return declared | wanted


def schema_tokens(schemas: Iterable[dict]) -> int:
    """Rough token cost of tool definitions: JSON characters / 4."""
    total = 0
    for schema in schemas or ():
        try:
            total += len(json.dumps(schema, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(repr(schema))
    return total // 4


def bounded_fits(schemas: List[dict]) -> bool:
    """Whether an allow-listed tool set is small enough to declare whole."""
    named = [s for s in schemas or [] if _name(s)]
    return bool(named) and len(named) <= BOUNDED_MAX_TOOLS and schema_tokens(named) <= BOUNDED_MAX_TOKENS


def declare(session_id: Optional[str], schemas: List[dict],
            full: Optional[List[dict]] = None,
            group: Optional[Callable[[List[str]], List[dict]]] = None) -> Tuple[List[dict], List[str]]:
    """``(tools to send, names callable this round)`` for one request.

    The tools to send are the chat's declared list with this round's schemas
    folded in (new ones appended, changed ones replaced in place); the callable
    names are exactly this round's schemas. An empty round (a tool-free
    wrap-up) still sends the declared list, with nothing callable.

    A tool this round withholds (a follow-up turn denies the launchers, a
    policy switch, the owner's baseline) stays declared but is not callable:
    the API refuses to call it and so does the executor. Dropping it would
    rewrite the tools array -- on 2026-09-29 a worker follow-up that withheld
    six launchers re-billed a ~100k-token chat from the start.

    ``full`` is a bounded session's whole allowed set (`bounded_fits`): it is
    declared before this round's schemas, so a later round that offers any tool
    of it only changes what is callable. The same applies to the cap below,
    which starts over from ``full`` plus the round, never from the round alone.

    ``group`` maps the names this round would add to schemas declared with them
    (the rest of a new tool's MCP server), so one change covers what the next
    rounds would otherwise add one or two at a time. 2026-10-02: a chat added
    tools of the same file server in three requests within a minute, each a
    full uncached re-read of the prompt.
    """
    active = [s for s in schemas or [] if _name(s)]
    allowed = [_name(s) for s in active]
    if not session_id:
        return list(active), allowed
    base = [s for s in full or [] if _name(s)]
    with _lock:
        entry = _load(session_id)
        before = list(entry.items())
        if group is not None:
            new_names = [_name(s) for s in active if _name(s) not in entry]
            if new_names:
                try:
                    extra = [s for s in group(new_names) or [] if _name(s)]
                except Exception:
                    logger.debug("[stable-tools] group expansion skipped", exc_info=True)
                    extra = []
                # Before this round's schemas, so the round's own copy wins a tie.
                base = base + [s for s in extra if _name(s) not in entry]
        replaced = []
        for schema in base + active:
            name = _name(schema)
            current = entry.get(name)
            if current is None or _callable_shape(current) != _callable_shape(schema):
                if current is not None:
                    replaced.append(name)
                entry[name] = schema
        if len(entry) > MAX_DECLARED:
            logger.info("[stable-tools] session=%s declared %d tools (cap %d); starting over from this round's %d",
                        session_id, len(entry), MAX_DECLARED, len(active))
            entry.clear()
            for schema in base + active:
                entry[_name(schema)] = schema
        changed = list(entry.items()) != before
        if changed:
            added = [n for n in entry if n not in dict(before)]
            if before:
                logger.info("[stable-tools] session=%s declared %d tools (+%d: %s)%s", session_id, len(entry),
                            len(added), ",".join(added[:8]) + ("…" if len(added) > 8 else ""),
                            # A parameter change rewrites the entry in place and breaks the
                            # cache; name it so a bundle shows which schema moved.
                            f" replaced={','.join(replaced[:8])}" if replaced else "")
            _save(session_id, entry)
        return list(entry.values()), allowed


def forget(session_id: str) -> None:
    """Drop a chat's declared list (tests, a deleted chat)."""
    with _lock:
        _DECLARED.pop(session_id, None)
    try:
        os.remove(_store_path(session_id))
    except OSError:
        pass


def reset_for_tests() -> None:
    with _lock:
        _DECLARED.clear()
