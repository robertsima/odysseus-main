"""
github_mcp_shaping.py

Keeps GitHub MCP list/search results small enough to stay in context.

github-mcp-server returns raw REST objects: every issue or PR carries its full
body, a user object with a dozen *_url fields, reactions, node ids and more.
Nothing asked for a page size either, so a single `search_issues` came back at
105k-203k characters and `list_pull_requests` at up to 270k. The GitHub inline
limit is 16k (tool_output_store), so every one of them was offloaded, and a
sub-agent looking for an issue spent whole rounds doing nothing but
`recall_tool_output` to fish the pieces back -- each round 5-18s of model time
and 100k+ prompt tokens.

Two cheap fixes, applied in the MCP dispatch path:

  * Request defaults. When the model gave no page size, ask for
    DEFAULT_PER_PAGE items, and turn on `minimal_output` when the server
    offers it. Only arguments the tool's advertised schema declares are ever
    added, and a value the model supplied is never replaced.
  * Result compaction. Rewrite each item down to what an agent picks the next
    call from (number, title, state, labels, author, dates, links) and cut
    long bodies, pointing at issue_read / pull_request_read for the full text.

Compaction only touches JSON whose shape it recognises; anything else, and any
error along the way, returns the original result unchanged.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tools whose results are lists of issues/PRs/commits/code hits/branches.
GITHUB_LIST_TOOLS = frozenset({
    "search_issues",
    "search_pull_requests",
    "list_issues",
    "list_pull_requests",
    "list_commits",
    "search_code",
    "list_branches",
})

# Single-item readers. Only their comment/review listings are compacted; the
# item itself (method "get") is the full text the truncation marker points to.
GITHUB_COMMENT_TOOLS = frozenset({"issue_read", "pull_request_read"})

DEFAULT_PER_PAGE = 10
BODY_MAX_CHARS = 600

# Page-size spellings a model might use; any of them means "the model chose".
_PAGE_SIZE_ARGS = ("perPage", "per_page")

# Wrapper keys that hold the item list in object-shaped payloads: search APIs
# use "items", the GraphQL-backed list_issues uses "issues".
_LIST_KEYS = ("items", "issues", "pull_requests", "commits", "branches")
# Top-level keys worth keeping next to that list.
_KEEP_TOP_LEVEL = ("total_count", "incomplete_results", "totalCount", "pageInfo")


def github_tool_name(qualified: str) -> str:
    """Bare tool name for a GitHub MCP tool (mcp__github*__name), else ""."""
    parts = (qualified or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[1].startswith("github"):
        return ""
    return parts[2]


def _schema_properties(mcp: Any, qualified: str) -> Dict:
    lookup = getattr(mcp, "get_tool_input_schema", None)
    if not callable(lookup):
        return {}
    schema = lookup(qualified)
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def github_mcp_request_defaults(
    qualified: str, args: Dict, mcp: Any
) -> Tuple[Dict, Optional[int]]:
    """Fill in page size / minimal_output for GitHub list and search tools.

    Returns (args, injected_per_page). args is a copy when anything was added;
    injected_per_page is the page size this function chose, or None when the
    model set one (or the schema has no perPage), so the result can say that
    the page was capped on the model's behalf.
    """
    if github_tool_name(qualified) not in GITHUB_LIST_TOOLS or not isinstance(args, dict):
        return args, None
    try:
        props = _schema_properties(mcp, qualified)
    except Exception as e:
        logger.debug("GitHub MCP schema lookup failed for %s: %s", qualified, e)
        return args, None
    shaped = dict(args)
    injected = None
    if "perPage" in props and all(shaped.get(k) is None for k in _PAGE_SIZE_ARGS):
        shaped["perPage"] = DEFAULT_PER_PAGE
        injected = DEFAULT_PER_PAGE
    if "minimal_output" in props and shaped.get("minimal_output") is None:
        shaped["minimal_output"] = True
    if shaped == args:
        return args, None
    return shaped, injected


# ── Result compaction ──

def _login(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("login") or value.get("name")
    if isinstance(value, str):
        return value
    return None


def _names(values: Any, key: str) -> Optional[List[str]]:
    if not isinstance(values, list):
        return None
    out = []
    for v in values:
        name = v.get(key) if isinstance(v, dict) else v
        if isinstance(name, str):
            out.append(name)
    return out


def _is_api_url(value: Any) -> bool:
    # api.github.com, or /api/v3/ on GitHub Enterprise Server.
    return isinstance(value, str) and ("//api." in value or "/api/v3/" in value)


def _truncate_body(body: Any, *, pointer: str) -> Any:
    if not isinstance(body, str) or len(body) <= BODY_MAX_CHARS:
        return body
    cut = len(body) - BODY_MAX_CHARS
    return f"{body[:BODY_MAX_CHARS]}… [{cut} more chars cut; {pointer} returns the full text]"


def _put(out: Dict, key: str, value: Any) -> None:
    if value is not None:
        out[key] = value


def _compact_issue(item: Dict) -> Dict:
    is_pr = "pull_request" in item or "head" in item or "merged_at" in item
    out: Dict[str, Any] = {}
    for key in ("number", "title", "state", "state_reason", "draft", "merged"):
        _put(out, key, item.get(key))
    url = item.get("html_url")
    if url is None and not _is_api_url(item.get("url")):
        url = item.get("url")
    _put(out, "html_url", url)
    if "pull_request" in item:
        out["is_pull_request"] = True
    _put(out, "user", _login(item.get("user") or item.get("author")))
    _put(out, "labels", _names(item.get("labels"), "name"))
    assignees = _names(item.get("assignees"), "login")
    if not assignees and item.get("assignee"):
        assignees = [a for a in [_login(item.get("assignee"))] if a]
    if assignees:
        out["assignees"] = assignees
    comments = item.get("comments")
    if isinstance(comments, int):
        out["comments"] = comments
    for side in ("head", "base"):
        ref = item.get(side)
        _put(out, side, ref.get("ref") if isinstance(ref, dict) else ref)
    milestone = item.get("milestone")
    _put(out, "milestone", milestone.get("title") if isinstance(milestone, dict) else milestone)
    for key in ("created_at", "updated_at", "closed_at", "merged_at"):
        _put(out, key, item.get(key))
    if item.get("body"):
        pointer = "pull_request_read" if is_pr else "issue_read"
        out["body"] = _truncate_body(item["body"], pointer=f'{pointer} (method "get")')
    return out


def _compact_commit(item: Dict) -> Dict:
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    message = commit.get("message") or item.get("message") or ""
    git_author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    out: Dict[str, Any] = {"sha": item.get("sha")}
    _put(out, "message", message.splitlines()[0] if isinstance(message, str) and message else None)
    _put(out, "author", _login(item.get("author")) or git_author.get("name"))
    _put(out, "date", git_author.get("date") or item.get("date"))
    _put(out, "html_url", item.get("html_url"))
    return out


def _compact_code_hit(item: Dict) -> Dict:
    repo = item.get("repository")
    out: Dict[str, Any] = {"path": item.get("path")}
    _put(out, "repository", repo.get("full_name") if isinstance(repo, dict) else repo)
    _put(out, "html_url", item.get("html_url"))
    return out


def _compact_branch(item: Dict) -> Dict:
    commit = item.get("commit")
    out: Dict[str, Any] = {"name": item.get("name")}
    _put(out, "sha", commit.get("sha") if isinstance(commit, dict) else None)
    _put(out, "protected", item.get("protected"))
    return out


def _compact_comment(item: Dict) -> Dict:
    # Comment bodies are NOT truncated: they are what get_comments was called
    # for, and no other tool returns them in full.
    out: Dict[str, Any] = {}
    for key in ("id", "state", "path", "line", "start_line", "side", "in_reply_to_id"):
        _put(out, key, item.get(key))
    _put(out, "user", _login(item.get("user") or item.get("author")))
    for key in ("created_at", "updated_at", "submitted_at", "html_url"):
        _put(out, key, item.get(key))
    _put(out, "body", item.get("body"))
    return out


def _item_compactor(item: Any, comments: bool):
    """Pick the compactor for one item, or None when its shape is unknown."""
    if not isinstance(item, dict):
        return None
    if comments:
        if "body" in item and "id" in item and "number" not in item:
            return _compact_comment
        return None
    if "number" in item and "title" in item:
        return _compact_issue
    if "sha" in item and ("commit" in item or "message" in item):
        return _compact_commit
    if "path" in item and ("repository" in item or "html_url" in item):
        return _compact_code_hit
    if "name" in item and isinstance(item.get("commit"), dict):
        return _compact_branch
    return None


def _compact_items(items: Any, comments: bool) -> Optional[List[Dict]]:
    if not isinstance(items, list) or not items:
        return None
    out = []
    for item in items:
        compactor = _item_compactor(item, comments)
        if compactor is None:
            return None
        out.append(compactor(item))
    return out


def _compact_payload(data: Any, comments: bool) -> Tuple[Any, int]:
    """(compacted payload, item count), or (None, 0) for an unknown shape."""
    if isinstance(data, list):
        items = _compact_items(data, comments)
        return (items, len(items)) if items is not None else (None, 0)
    if not isinstance(data, dict) or comments:
        return None, 0
    list_keys = [k for k in _LIST_KEYS if isinstance(data.get(k), list)]
    if len(list_keys) != 1:
        return None, 0
    items = _compact_items(data[list_keys[0]], comments)
    if items is None:
        return None, 0
    out = {k: data[k] for k in _KEEP_TOP_LEVEL if k in data}
    out[list_keys[0]] = items
    return out, len(items)


def _count_items(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            if isinstance(data.get(key), list):
                return len(data[key])
    return 0


def compact_github_mcp_result(
    qualified: str, result: Any, injected_per_page: Optional[int] = None
) -> Any:
    """Shrink a GitHub list/search MCP result; the original on anything unexpected."""
    try:
        tool = github_tool_name(qualified)
        comments = tool in GITHUB_COMMENT_TOOLS
        if not (tool in GITHUB_LIST_TOOLS or comments):
            return result
        if not isinstance(result, dict) or result.get("exit_code") != 0:
            return result
        text = result.get("stdout")
        if not isinstance(text, str) or not text.strip():
            return result
        try:
            data = json.loads(text)
        except ValueError:
            return result
        compacted, count = _compact_payload(data, comments)
        new_text = text
        if compacted is not None:
            candidate = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
            if len(candidate) < len(text):
                logger.info(
                    "Compacted GitHub MCP result %s: %d -> %d chars",
                    qualified, len(text), len(candidate),
                )
                new_text = candidate
        if compacted is None:
            count = _count_items(data)
        if injected_per_page and count >= injected_per_page:
            # The model never asked for a page size, so say the list may go on;
            # a bare array of 10 PRs otherwise reads as "the repo has 10 PRs".
            new_text += (
                f"\n[Showing {count} results per page because no perPage was given. "
                "Request the next page, or pass a larger perPage, for more.]"
            )
        if new_text == text:
            return result
        shaped = dict(result)
        shaped["stdout"] = new_text
        return shaped
    except Exception as e:
        logger.warning("GitHub MCP result compaction failed for %s: %s", qualified, e)
        return result
