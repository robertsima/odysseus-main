"""
tool_schemas.py

OpenAI-compatible function tool schemas and the converter that turns
native function calls back into ToolBlocks for the execution pipeline.

Extracted from agent_tools.py to keep schema definitions separate from
tool parsing / execution logic.
"""

import copy
import json
import logging
from typing import Optional

from src.tool_types import ToolBlock, TOOL_TAGS
from src.tool_parsing import _TOOL_NAME_MAP
from src.tool_security import BUILTIN_EMAIL_TOOLS

logger = logging.getLogger(__name__)


_REQUIRED_NATIVE_TOOL_ARGS = {
    "web_search": ("query", "queries"),
    "web_fetch": ("url",),
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "apply_patch": ("patch_text", "patchText", "patch"),
}


# What a native model reads about a tool is its schema: the text-fence prompt
# lists only names on these routes. The first compaction (2026-09-09, when a
# chat's tool list still changed every turn and schemas were 38-81% of prompt
# tokens) kept the text before the first ". " (at most 220 chars) and dropped
# every parameter description, so `bash` arrived as "Run a shell command (full
# access)", `ask_user` and `web_fetch` were cut at "(e.g", and "default 60, max
# 3600" style facts never reached a model. With the tool list held fixed and
# cached (src/stable_tools.py; 91% cache hits on 2026-09-30) that prose is paid
# at the cached rate, so the standard level keeps the leading whole sentences
# of each description and each parameter's first sentences. The lean level is
# for small windows (local models), where the whole schema block is uncached
# context: the tool's first sentences and no parameter prose.
#
# 2026-10-01: 29 native tools had text past these budgets, so the model read a
# clipped copy nobody had reviewed (the routing rule sat after the cut).
# Descriptions are now written to fit, with the routing text first, and
# tests/test_tool_schema_budget.py fails when one does not: the clip is a
# safety net for MCP tools, and for native tools the source is what ships.
_COMPACT_TOOL_DESCRIPTION_CHARS = 400
_COMPACT_PARAM_DESCRIPTION_CHARS = 160
_LEAN_TOOL_DESCRIPTION_CHARS = 220
# A period after these is not the end of a sentence.
_ABBREVIATIONS = ("e.g", "i.e", "etc", "vs", "approx", "incl", "cf", "no", "min", "max")
_UNTRIMMED_TOOLS = frozenset({"manage_git", "manage_agent_worktree"})


def _sentence_ends(text: str):
    """Indexes just past each sentence end in ``text`` (". ", "! ", "? ", "\n")."""
    for i, ch in enumerate(text):
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if ch == "\n":
            yield i
        elif ch in ".!?" and nxt.isspace():
            word = text[max(0, text.rfind(" ", 0, i) + 1):i].lstrip("(").lower()
            if ch == "." and word in _ABBREVIATIONS:
                continue
            yield i + 1


def _clip_prose(text: str, limit: int) -> str:
    """Whole sentences of ``text`` that fit ``limit``; one cut sentence if none do."""
    text = " ".join(str(text or "").split()) if "\n" not in str(text or "") else str(text).strip()
    if len(text) <= limit:
        return text
    cut = 0
    for end in _sentence_ends(text):
        if end > limit:
            break
        cut = end
    if cut:
        return text[:cut].strip()
    head = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:(")
    return head + "…"


def _compact_schema_prose(value, limit: int, *, is_properties: bool = False):
    """Clip (``limit`` > 0) or drop (``limit`` == 0) prose in a JSON schema in
    place, keeping its entire shape.

    Keys of a ``properties`` mapping are parameter NAMES, so a parameter
    literally called ``description`` (calendar notes, a skill's summary, an
    issue body on an MCP server) is a schema, never prose to remove. The
    previous stripper popped it and the parameter vanished from the payload.
    """
    if isinstance(value, dict):
        if is_properties:
            for child in value.values():
                _compact_schema_prose(child, limit)
            return
        prose = value.get("description")
        if isinstance(prose, str):
            if limit > 0:
                value["description"] = _clip_prose(prose, limit)
            else:
                value.pop("description")
        for key, child in list(value.items()):
            if key != "description":
                _compact_schema_prose(child, limit, is_properties=(key == "properties"))
    elif isinstance(value, list):
        for child in value:
            _compact_schema_prose(child, limit)


def compact_function_tool_schemas(schemas, *, lean: bool = False):
    """Return compact provider-payload copies without changing callable shape.

    Canonical schemas remain the execution contract. Descriptions keep their
    leading whole sentences, which carry what the tool is for and when to use
    it; multi-action Git tools keep all of their action/field mapping. ``lean``
    is the small-window level: shorter descriptions and no parameter prose.
    """
    tool_limit = _LEAN_TOOL_DESCRIPTION_CHARS if lean else _COMPACT_TOOL_DESCRIPTION_CHARS
    param_limit = 0 if lean else _COMPACT_PARAM_DESCRIPTION_CHARS
    compact = []
    for schema in schemas or []:
        item = copy.deepcopy(schema)
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict):
            compact.append(item)
            continue
        if fn.get("name") in _UNTRIMMED_TOOLS:
            compact.append(item)
            continue
        description = str(fn.get("description") or "").strip()
        if description:
            fn["description"] = _clip_prose(description, tool_limit)
        # Nested object/array schemas are common in MCP tools. Only prose is
        # clipped: ``type``, ``required``, ``enum``, ``items``, and every other
        # JSON-schema constraint stay intact.
        _compact_schema_prose(fn.get("parameters"), param_limit)
        compact.append(item)
    return compact

# ---------------------------------------------------------------------------
# OpenAI-compatible function tool schemas
# ---------------------------------------------------------------------------
FUNCTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "discover_tools",
            "description": "Attach tools that are not in your list: describe what you need ('read a Todoist task', 'render a Penpot board'). Matches are callable on your next round, at most 8.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "The capability you need, in plain words."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command (on the host or in a workspace sandbox, per this chat's shell note): installs, builds, tests, git, programs. Make `#!bg` the first line to run a long command in the background.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"},
                    "idle_timeout": {"type": "integer", "description": "Seconds of silence before the command is stopped (default 60, max 3600). Raise it for quiet builds."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "python",
            "description": "Execute Python code to compute a result or test something. Prefer a dedicated tool whenever one fits the job (reading, writing, or searching files); use python only for computation, data processing, or scripting no dedicated tool covers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Quick lookup of one fact or current event. Longer 'research X' jobs go to deep research (trigger_research) when it is available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "time_filter": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Optional freshness filter for news/latest/today queries"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a known URL as readable text (HTML, JSON, SVG, plain text). A '[partial content: ...]' notice means the body was cut short; call again with full=true for the rest. To find pages, use web_search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL or domain to fetch (http/https; a bare domain like example.com is fine)"},
                    "full": {"type": "boolean", "description": "Raise the download budget to the hard cap. Use it after a result reported partial content."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file: its first 20,000 characters, or lines offset to offset+limit. When the result says truncated, call again with offset set to the line it names.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "offset": {"type": "integer", "description": "1-based line to start reading from (optional)"},
                    "limit": {"type": "integer", "description": "Max number of lines to read from offset (optional)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents by regex (ripgrep, .gitignore respected). Returns file:line:match, at most 200 hits. Pass the narrowest `path` and a `glob` for a large tree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (optional; defaults to the project root)"},
                    "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py' (optional)"},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match (optional)"},
                    "max_results": {"type": "integer", "description": "Max matches to return (optional)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob (recursive), newest first, at most 200. To see a tree's layout, use `ls` with depth, then glob inside the folder that matters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.ts' or 'src/**/test_*.py'"},
                    "path": {"type": "string", "description": "Base directory (optional; defaults to the project root)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List a directory (dotfiles hidden, at most 200 entries). depth 2-4 returns a folder outline with file counts, skipping build, vendor and cache folders; use it as the first look at an unfamiliar tree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (optional; defaults to the project root)"},
                    "depth": {"type": "integer", "description": "1 (default) lists entries; 2-4 returns a folder outline to that depth"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "preview_file",
            "description": "See your output: renders an HTML, SVG or raster file from the workspace or its worktree to a screenshot. Open it before reporting visual work done and compare it with the reference image or the request, since passing tests do not show the shape is right. Read-only; network is blocked, so backend and CDN content renders partially.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to render (workspace-relative, or absolute inside it): .html, .svg, .png, .jpg, .webp, .gif"},
                    "width": {"type": "integer", "description": "Viewport width in px (default 1280, SVG 512, max 2560)"},
                    "height": {"type": "integer", "description": "Viewport height in px (default 800, SVG 512, max 2560)"},
                    "color_scheme": {"type": "string", "enum": ["light", "dark"], "description": "prefers-color-scheme to emulate (default light). Check both when the page has a dark theme."},
                    "scale": {"type": "integer", "description": "Device pixel ratio 1-4 (default 1). Use 2-4 for small assets such as icons."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace",
            "description": "Return the absolute path of the active workspace folder. File tools are confined to it; the shell starts there but is not sandboxed. Call this first when the user says 'the project', 'the code' or 'this folder' without a path, instead of asking.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file on disk. To change part of an existing file, use edit_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "File content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in a file on disk. `old_string` must match once, indentation included, or set replace_all. write_file creates a new file; edit_document edits editor-panel documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact text to replace (must match the file, including indentation)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply related edits across files in one patch (*** Begin Patch ... *** End Patch with Add File, Update File and Delete File sections). Each hunk's context lines must match the file exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_text": {
                        "type": "string",
                        "description": "The whole patch, from *** Begin Patch to *** End Patch"
                    }
                },
                "required": ["patch_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "Create and maintain a structured task list for the current coding session. Use during multi-step implementation/debug/refactor work and keep statuses current.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Current task list. Only one item should be in_progress.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Task description"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                "priority": {"type": "string", "enum": ["low", "medium", "high"]}
                            },
                            "required": ["content", "status"]
                        }
                    }
                },
                "required": ["todos"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_git",
            # Action-dependent optional fields must not become all-required.
            "strict": False,
            "description": (
                "Git in approved local checkouts, no shell needed; send only the fields the chosen action uses. Start with action=repositories for the absolute paths. Read: status, diff, log, branches, remotes. Change: stage (explicit relative paths), unstage, commit, branch, tag, switch, fetch, fetch_branch, pull, pull_with_restore, stash_list/create/apply/pop, push, merge, reset, rebase, set_upstream, clone, init. Pull and merge are fast-forward only. Push, merge, reset and rebase need expected_head and a fresh human confirmation. Policy refuses every delete (branches, remote branches, stashes), force push and discarding work. reset refuses a dirty tree and rebase aborts on conflict. set_upstream binds the current branch to its pushed remote_branch and never replaces an upstream. clone takes GitHub HTTPS sources only. diff covers the whole tree and is capped at 256 KB; for one file run `git diff -- <path>` in bash. Linked worktrees (.git is a file) allow only the read actions; commit there with manage_agent_worktree. Publishing a change for review also goes through manage_agent_worktree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["repositories", "status", "diff", "log", "branches", "remotes", "clone", "init", "stage", "unstage", "commit", "branch", "tag", "switch", "fetch", "fetch_branch", "pull", "pull_with_restore", "stash_list", "stash_create", "stash_apply", "stash_pop", "push", "merge", "reset", "rebase", "set_upstream"]},
                    "repository": {"type": "string", "description": "All actions except repositories: absolute checkout path; clone/init use the new target path"},
                    "source": {"type": "string", "description": "clone only: credential-free https://github.com/owner/repository URL (or the configured GitHub Enterprise host)"},
                    "branch": {"type": "string", "description": "clone only: optional remote branch"},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "clone only: optional shallow history depth"},
                    "initial_branch": {"type": "string", "description": "init only: initial branch, default main"},
                    "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 100, "description": "stage/unstage only: exact relative file paths; no globs"},
                    "name": {"type": "string", "description": "Branch/tag name (branch/tag/switch)"},
                    "ref": {"type": "string", "description": "Existing revision for log, branch, tag or merge"},
                    "message": {"type": "string", "description": "Commit or stash_create message"},
                    "index": {"type": "integer", "minimum": 0, "maximum": 99, "description": "stash action only: stash index, default 0"},
                    "author_name": {"type": "string", "description": "commit only: omit to use local Git identity"},
                    "author_email": {"type": "string", "description": "commit only: omit to use local Git identity"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "log only: maximum commits (default 20)"},
                    "staged": {"type": "boolean", "description": "diff only: compare index versus HEAD (default false)"},
                    "remote_branch": {"type": "string", "description": "fetch_branch/push/set_upstream: configured remote's branch name"},
                    "remote": {"type": "string", "description": "fetch_branch/set_upstream only: existing configured remote name; omit when origin or one remote is unambiguous"},
                    "expected_head": {"type": "string", "description": "Exact current HEAD being confirmed for publish/integration/history rewrite"},
                    "expected_target": {"type": "string", "description": "Exact target/stash/remote-lease commit being confirmed; 40 zeros means absent remote for force-with-lease"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_agent_worktree",
            "strict": False,
            "description": (
                "Isolated, human-gated publishing worktree for a repository; send only the fields the chosen action uses. start (name = task name, base = origin/main, a branch or a SHA) makes branch agent/<repo>/<name>; status, diff and commit work in it; request_publish freezes the change; a person approves it in the Odysseus UI (pushes nothing); show_request and list_requests follow it; checks reads the open PR's CI (wait_seconds waits for it to finish in one call), marking failures that also fail on the base; cleanup removes a clean worktree. Pass `repository` (absolute path from manage_git repositories) for any project, and the same value on later calls; omit it only for the Odysseus source checkout. publish needs a request_id and an approval_code a person gives you; you cannot approve your own change. repo_list, repo_status and repo_pull are legacy: use manage_git."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "start", "commit", "diff", "request_publish",
                                 "publish", "list_requests", "show_request", "checks", "cleanup",
                                 "repo_list", "repo_status", "repo_pull"],
                        "description": "Default status. repo_list, repo_status and repo_pull are legacy; use manage_git for repository listing and sync."
                    },
                    "repository": {"type": "string", "description": "Absolute checkout path from manage_git repositories, not a URL. Omit only for the Odysseus source checkout."},
                    "name": {"type": "string", "description": "start only: task name; the branch becomes agent/<repo>/<name> (agent/odysseus/<name> without repository)"},
                    "branch": {"type": "string", "description": "Full agent branch, when it already exists (checks, diff, commit...). Not a base such as origin/main"},
                    "wait_seconds": {"type": "integer", "description": "checks only: wait up to this many seconds (0-900, default 0) for running checks to finish; ends early when the user writes"},
                    "base": {"type": "string", "description": "start only: existing ref or commit the new branch starts from (origin/main, a branch, or a SHA). Default: the configured base (Odysseus) or origin/HEAD"},
                    "expected_base": {"type": "string", "description": "start only: full commit SHA base must resolve to; start refuses on mismatch"},
                    "message": {"type": "string", "description": "Commit message (action=commit)"},
                    "title": {"type": "string", "description": "Draft PR title (action=request_publish)"},
                    "body": {"type": "string", "description": "Draft PR body (action=request_publish)"},
                    "request_id": {"type": "string", "description": "Approval request id"},
                    "approval_code": {
                        "type": "string",
                        "description": "One-time code a human produced with the operator CLI (action=publish)"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_app_logs",
            "description": (
                "Read Odysseus's own application logs to debug the running app. list shows the log files; tail returns the last lines of one (filter by substring or level); trace with an id (workflow, run or session) gathers every line and run record that mentions it; bundle writes a diagnostics zip (logs plus configs, never message text) and returns its path. Credentials are redacted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "tail", "trace", "bundle"], "description": "Default: tail"},
                    "id": {"type": "string", "description": "trace: the workflow-, run or session ID to audit. bundle: an extra chat (session ID) to include."},
                    "name": {"type": "string", "description": "Log file name, e.g. app.log. Defaults to the app log."},
                    "lines": {"type": "integer", "description": "How many lines to return (1-500, default 100)"},
                    "contains": {"type": "string", "description": "Only lines containing this substring"},
                    "since_minutes": {"type": "number", "description": "Only entries from the last N minutes, e.g. 10 for 'the last 10 minutes' (bundle: its window, default 60)"},
                    "level": {
                        "type": "string",
                        "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        "description": "Minimum log level to include"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "Create a new document in the editor panel for code, scripts, apps, games or any long-form or structured content longer than a short paragraph, when no open document or email draft is the target. If an email compose draft is open, edit that draft instead. Put large generated content here, not in chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "language": {"type": "string", "description": "Programming language or format (e.g. python, javascript, markdown, text)"},
                    "content": {"type": "string", "description": "The document content"}
                },
                "required": ["title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_document",
            "description": "Edit a document in the editor panel (not a file on disk; use edit_file for paths like ~/x.txt). Targets the document open in this chat, or the one named by document_id. Send FIND/REPLACE pairs, several per call, for any edit smaller than a full rewrite.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "Document id from manage_documents list, or its #document-<id> link. Required unless the document is open in this chat's editor. Titles are not unique."},
                    "edits": {
                        "type": "array",
                        "description": "List of find/replace edits (first match only per edit)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "Exact text to find in the document"},
                                "replace": {"type": "string", "description": "Text to replace it with"}
                            },
                            "required": ["find", "replace"]
                        }
                    }
                },
                "required": ["edits"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_document",
            "description": "Suggest improvements to the document open in this chat (or the one named by document_id) WITHOUT editing it. Creates inline comment bubbles the user can accept or reject. Use when the user asks for suggestions, review, improvements, or feedback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "Document id from manage_documents list, or its #document-<id> link. Required unless the document is open in this chat's editor. Titles are not unique."},
                    "suggestions": {
                        "type": "array",
                        "description": "List of suggested changes with reasons",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "Exact text in the document to suggest changing"},
                                "replace": {"type": "string", "description": "Suggested replacement text"},
                                "reason": {"type": "string", "description": "Brief explanation of why this change helps"}
                            },
                            "required": ["find", "replace", "reason"]
                        }
                    }
                },
                "required": ["suggestions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_document",
            "description": "Replace the ENTIRE document open in this chat (or the one named by document_id). ONLY use for genuine full rewrites (>50% of lines changed). For any smaller change, use edit_document — echoing back the whole file for small edits is wasteful.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "Document id from manage_documents list, or its #document-<id> link. Required unless the document is open in this chat's editor. Titles are not unique."},
                    "content": {"type": "string", "description": "Complete new document content"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall_tool_output",
            "description": (
                "Read back a stored tool output: a `toolout-...` ref from a truncated result, or an `evt-...` ref from an earlier turn's work record. Pass `ref` for the whole stored output (long ones page: call again with the `offset` each page names) or add `query` to search it, rather than re-running the tool. No arguments lists what is stored."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "A `toolout-...` or `evt-...` reference.",
                    },
                    "query": {
                        "type": "string",
                        "description": "What you need from that output, in natural language. Matched semantically against the stored text.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset for an ordered read (use instead of `query` to page through the output).",
                    },
                    "limit": {
                        "type": "integer",
                        # Must match _RECALL_SLICE_CHARS / _RECALL_MAX_SLICE_CHARS
                        # in agent_tools/rag_tools.py, which is what actually
                        # clamps. This said "max 12000" after the ceiling
                        # dropped to 8000, so the model asked for more than it
                        # could get (one round asked for 20,000), silently
                        # received a shorter slice and had to page again.
                        # Kept a plain literal on purpose: this whole structure
                        # is read statically by ast.literal_eval in
                        # test_tool_index_schema_parity. The numbers are tied to
                        # the constants by test instead.
                        "description": "Omit to get the whole output (pages of 20000 characters when it is longer). An explicit limit returns that many characters, max 8000 (3000 if not a number).",
                    },
                    "k": {
                        "type": "integer",
                        "description": "How many matching excerpts to return for a query (default 5, max 12).",
                    },
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall_chat_history",
            "description": (
                "Read this chat's own earlier messages, including ones compacted out of your context. No arguments: an overview. `query`: search every message and tool output. `message`: one message in full by #index or id, with `before`/`after` neighbours. `start` and `count`: a range. Use it instead of asking the user to repeat something or re-running a command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words to find in this chat's messages and tool outputs (all matches are ranked; exact phrases rank first).",
                    },
                    "message": {
                        "type": "string",
                        "description": "A message to read in full: its #index (e.g. \"#12\") or id.",
                    },
                    "before": {
                        "type": "integer",
                        "description": "With `message`: how many earlier messages to include (max 40).",
                    },
                    "after": {
                        "type": "integer",
                        "description": "With `message`: how many later messages to include (max 40).",
                    },
                    "start": {
                        "type": "integer",
                        "description": "Read a range from this #index (0 is the chat's first message).",
                    },
                    "count": {
                        "type": "integer",
                        "description": "With `start`: how many messages (default 10, max 40).",
                    },
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Semantic search over the user's indexed personal documents (vault, notes, journal, uploaded files). The default way to answer a question about them: returns relevant excerpts with the file path of each. Read a whole file (read_file with offset/limit on a returned path) only when an excerpt is not enough."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, in natural language. Matched semantically, so phrase it as the question or topic rather than a filename.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "How many excerpts to return (default 5, max 12). Raise it only when the answer is likely spread across several notes.",
                    },
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_chats",
            "description": "Search the user's past session transcripts by keyword. Use when the user asks about previous chats, past conversations, or when direct transcript evidence is better than persistent memory. Returns matching sessions with clickable links and nearby context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword(s) to find in past conversations"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chat_with_model",
            "description": "Send a message to another AI model and get its response. Use for getting a second opinion, delegating subtasks, or AI-to-AI communication.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Model name (e.g. 'qwen3-32b') or model@endpoint_name"},
                    "message": {"type": "string", "description": "The message to send to the model"}
                },
                "required": ["model", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_session",
            "description": "Create a new chat for ongoing conversations with a specific model. (The UI calls these 'chats'; 'session' is the internal term.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new chat"},
                    "model": {"type": "string", "description": "Model name or model@endpoint_name"}
                },
                "required": ["name", "model"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List the user's chats (the UI calls them 'chats') as clickable markdown links. Use this to enumerate chats before opening, renaming, archiving, or deleting them. When replying to the user, preserve the returned [title](#session-id) links; do not strip them into plain text. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional keyword to filter chats by name"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_session",
            "description": "Send a message to an existing live chat and get its model's response. mode='agent' lets that chat's agent work the message with its own tools as a sub-agent and returns its final answer; mode='chat' (default) is one model reply. To read or search old chats, use search_chats or list_sessions instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The id of the chat to send the message to, or \"new\" to start a fresh sub-agent chat for this task"},
                    "message": {"type": "string", "description": "The message to send (for a new sub-agent: the complete task, since it starts with no context)"},
                    "mode": {"type": "string", "enum": ["chat", "agent"], "description": "chat = one model reply (default); agent = run the target chat's agent with tools as a sub-agent"},
                    "profile": {"type": "string", "description": "Agent profile name (Settings > Workbench). Implies mode=agent and requires session_id 'new'; profile_scope in the result says what happened."},
                    "workspace": {"type": "string", "description": "agent mode: the checkout the sub-agent's file tools work in (a path from get_workspace). Required when this chat has none and the task touches a repository."},
                    "requires": {"type": "array", "items": {"type": "string", "enum": ["workspace", "write", "read_only", "no_workspace"]}, "description": "agent mode: what the task needs. The sub-agent is refused before it starts, with the fix, when a need cannot be met."}
                },
                "required": ["session_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pipeline",
            "description": "Run a multi-step AI pipeline where each model's output feeds the next. Example: Draft -> Critique -> Revise.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Pipeline steps in order",
                        "items": {
                            "type": "object",
                            "properties": {
                                "model": {"type": "string", "description": "Model name for this step"},
                                "instruction": {"type": "string", "description": "What this step should do"}
                            },
                            "required": ["model", "instruction"]
                        }
                    }
                },
                "required": ["steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_session",
            "description": "Manage a chat: rename, archive, unarchive, delete, mark important, truncate, fork; `running` shows what each working chat is doing (tool, command, duration, last output) and `stop` halts one. Use running when the user asks whether an agent is stuck or busy, instead of starting another. For delete, pass the exact id from list_sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["rename", "archive", "unarchive", "delete", "important", "unimportant", "truncate", "fork", "running", "stop"],
                               "description": "The action to perform. running: session_id optional ('all' or omitted = every working chat). stop: stops that chat's running turn and its workers."},
                    "session_id": {"type": "string", "description": "Exact target chat id from list_sessions (or running), or 'current' for the active chat where supported"},
                    "value": {"type": "string", "description": "Action parameter: new name (rename), keep_count (truncate/fork)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": "Memory: durable facts, events, contacts and preferences that outlive this chat. add when the user states something to remember or a lasting preference; search before asking the user for something they may already have told you; edit or delete by memory_id from list or search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "edit", "delete", "search"],
                               "description": "The action to perform"},
                    "text": {"type": "string", "description": "Memory text (for add/edit) or search query (for search)"},
                    "memory_id": {"type": "string", "description": "Memory ID (for edit/delete)"},
                    "category": {"type": "string", "enum": ["fact", "event", "contact", "preference"],
                                 "description": "Memory category (for add/list filter)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": "List all available AI models across configured endpoints. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional keyword to filter models"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ui_control",
            "description": "Control the UI: toggle tools, open_panel, open_email_reply (opens a draft, does not send; put the drafted text in body), set_mode, switch_model, get_toggles, set_theme (presets: dark, light, midnight, paper, cyberpunk, retrowave, forest, ocean, ume, copper, terminal, organs, lavender, gpt, claude, cute), create_theme (name plus hex colors, for any theme not in the presets; applies at once).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["toggle", "open_panel", "open_email_reply", "set_mode", "switch_model", "set_theme", "create_theme", "get_toggles"],
                               "description": "The UI action. Use set_theme for presets, create_theme to build a custom theme with any hex colors"},
                    "name": {"type": "string", "description": "toggle: web, bash, research, incognito, document_editor. open_panel: documents, gallery, email, sessions, notes, brain, skills, settings, cookbook."},
                    "value": {"type": "string", "description": "Value: on/off for toggle, agent/chat for set_mode, model name for switch_model, theme name for set_theme, or folder for open_email_reply"},
                    "uid": {"type": "string", "description": "Email UID for open_email_reply"},
                    "folder": {"type": "string", "description": "Email folder for open_email_reply (default INBOX)"},
                    "mode": {"type": "string", "description": "Reply draft mode for open_email_reply: reply, reply-all, or ai-reply"},
                    "body": {"type": "string", "description": "For open_email_reply: reply body to pre-fill. Required whenever the user told you what the reply should say. Opens a draft, does not send."},
                    "colors": {"type": "object", "description": "For create_theme: the theme colors",
                               "properties": {
                                   "bg": {"type": "string", "description": "Background color (hex, e.g. #1a1a2e)"},
                                   "fg": {"type": "string", "description": "Foreground/text color (hex)"},
                                   "panel": {"type": "string", "description": "Panel/sidebar background color (hex)"},
                                   "border": {"type": "string", "description": "Border/divider color (hex)"},
                                   "accent": {"type": "string", "description": "Accent color for buttons, brand, highlights (hex)"},
                                   "userBubbleBg": {"type": "string", "description": "User chat bubble background (hex, optional)"},
                                   "aiBubbleBg": {"type": "string", "description": "AI chat bubble background (hex, optional)"},
                                   "bubbleBorder": {"type": "string", "description": "Chat bubble border color (hex, optional)"},
                                   "sidebarBg": {"type": "string", "description": "Sidebar background override (hex, optional)"},
                                   "sectionAccent": {"type": "string", "description": "Section header accent color (hex, optional)"},
                                   "brandColor": {"type": "string", "description": "Brand/logo color (hex, optional)"},
                                   "inputBg": {"type": "string", "description": "Chat input background (hex, optional)"},
                                   "inputBorder": {"type": "string", "description": "Chat input border (hex, optional)"},
                                   "sendBtnBg": {"type": "string", "description": "Send button background (hex, optional)"},
                                   "sendBtnHover": {"type": "string", "description": "Send button hover color (hex, optional)"},
                                   "codeBg": {"type": "string", "description": "Code block background (hex, optional)"},
                                   "codeFg": {"type": "string", "description": "Code block text color (hex, optional)"},
                                   "toggleBg": {"type": "string", "description": "Toggle switch off background (hex, optional)"},
                                   "toggleActive": {"type": "string", "description": "Toggle switch on color (hex, optional)"},
                                   "accentPrimary": {"type": "string", "description": "Primary accent override (hex, optional)"},
                                   "accentError": {"type": "string", "description": "Error/danger color (hex, optional)"}
                               },
                               "required": ["bg", "fg", "panel", "border", "accent"]}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask a multiple-choice question when the answer changes what you do next and a default would be a guess (approach, assumption, target). Calling it ends your turn; the user's pick arrives as the next message. Destructive actions have their own confirmation flow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Specific and self-contained."},
                    "options": {
                        "type": "array",
                        "description": "2-6 choices, each a short `label` with an optional one-line `description`.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Choice text, 1-5 words."},
                                "description": {"type": "string", "description": "One-line trade-off."}
                            },
                            "required": ["label"]
                        }
                    },
                    "multi": {"type": "boolean", "description": "True when several options may be picked."}
                },
                "required": ["question", "options"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Keep this chat's task checklist as markdown. Write it when a request has several steps or an approved plan is running, tick each step `- [x]` as you finish it, rewrite it when the request changes. It is shown to you on later turns until every step is ticked. Send the complete checklist every time; an empty plan clears it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "description": "The full checklist, one step per line: `- [ ]` pending, `- [x]` done. An empty string clears it."}
                },
                "required": ["plan"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Manage scheduled tasks: list, create, edit, delete, pause, resume, run. Use it for any recurring request ('every morning...', 'daily at 7:30') instead of doing it once. Types: llm (runs a prompt), research (deep-research on a question), action (built-in automation). Triggers are time-based or event-based.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "edit", "delete", "pause", "resume", "run"],
                               "description": "The action to perform"},
                    "task_id": {"type": "string", "description": "Task ID (for edit/delete/pause/resume/run)"},
                    "name": {"type": "string", "description": "Task name"},
                    "prompt": {"type": "string", "description": "The instruction (for task_type=llm) or the research question (for task_type=research). Required for both."},
                    "task_type": {"type": "string", "enum": ["llm", "research", "action"],
                                  "description": "llm = AI runs your prompt; research = runs the deep-research pipeline on the prompt as a question; action = direct built-in function"},
                    "action_name": {"type": "string", "enum": [
                        "tidy_sessions", "tidy_documents", "consolidate_memory", "tidy_research",
                        "summarize_emails", "draft_email_replies", "extract_email_events",
                        "classify_events", "learn_sender_signatures",
                        "test_skills", "audit_skills", "check_email_urgency"
                    ],
                                    "description": "Built-in action (for task_type=action)"},
                    "trigger_type": {"type": "string", "enum": ["schedule", "event"],
                                     "description": "schedule = time-based, event = count-based"},
                    "schedule": {"type": "string", "enum": ["once", "daily", "weekly", "monthly"],
                                 "description": "Schedule frequency (for trigger_type=schedule)"},
                    "scheduled_time": {"type": "string", "description": "HH:MM in UTC (for schedule triggers). Convert the user's stated local time using the UTC offset given in the 'Current date and time' context."},
                    "scheduled_day": {"type": "integer", "description": "Day of week 0=Mon (weekly) or day of month (monthly)"},
                    "trigger_event": {"type": "string", "enum": ["session_created", "message_sent", "document_created", "memory_added", "research_completed", "email_received", "skill_added"],
                                      "description": "Event name (for trigger_type=event)"},
                    "trigger_count": {"type": "integer", "description": "Fire every N events (for trigger_type=event)"},
                    "output_target": {"type": "string", "description": "Where results go (default 'session': a dedicated chat the user reads). Use an email tool name only when the user asked to be emailed and an address is known."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_calendar",
            "description": "Manage calendar events: list_events in a date range, create_event, update_event, delete_event, list_calendars. Resolve relative dates against the 'Current date and time' context, then pass ISO 8601 datetimes in the user's local time (all_day=true with YYYY-MM-DD). reminder_minutes creates the reminder note, so skip manage_notes for it. Set rrule only when the user asks for recurrence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list_events", "create_event", "update_event", "delete_event", "list_calendars"],
                               "description": "Action to perform"},
                    "summary": {"type": "string", "description": "Event title (for create/update)"},
                    "dtstart": {"type": "string", "description": "Start ISO datetime, or YYYY-MM-DD if all_day"},
                    "dtend": {"type": "string", "description": "End ISO datetime; defaults to +1h (or +1 day for all_day)"},
                    "all_day": {"type": "boolean", "description": "Whether this is an all-day event"},
                    "description": {"type": "string", "description": "Event description / notes"},
                    "location": {"type": "string", "description": "Event location"},
                    "uid": {"type": "string", "description": "Event UID (for update/delete)"},
                    "calendar_href": {"type": "string", "description": "Calendar id from list_calendars (a name or short id prefix also works; not a CalDAV URL). Default: the first calendar on create, all on list."},
                    "calendar": {"type": "string", "description": "Alias for calendar_href; same id, name or short-id prefix."},
                    "start": {"type": "string", "description": "list_events range start (ISO datetime). Resolve month or week requests to a range first; a loose query string does not work."},
                    "end": {"type": "string", "description": "list_events range end (ISO datetime). Defaults to +14 days when no range is given."},
                    "event_type": {"type": "string", "description": "Tag / category for the event. Common values: work, personal, health, travel, meal, social, admin, other. Aliases accepted: tag, category, type."},
                    "importance": {"type": "string", "enum": ["low", "normal", "high", "critical"], "description": "Priority level (defaults to 'normal')"},
                    "reminder_minutes": {"type": "integer", "description": "For create_event: create an Odysseus reminder this many minutes before the event, e.g. 5 for 'reminder 5 min before'."},
                    "rrule": {"type": "string", "description": "iCalendar RRULE, e.g. 'FREQ=WEEKLY;BYDAY=MO'. On update_event an empty string removes recurrence."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_notes",
            "description": "Manage notes and checklists: list, search, view, add, update, delete, toggle_item. Find a note with list or search, then view it by id for the full body. A to-do list is note_type='checklist' with the items in `checklist_items`, never in `content`; a freeform note is note_type='note' with `content`. due_date fires a notification, so skip a calendar event for the same reminder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "search", "view", "add", "update", "delete", "toggle_item"],
                               "description": "The action to perform"},
                    "id": {"type": "string", "description": "Note id (for update/delete/toggle_item); 8-char prefix is fine"},
                    "query": {"type": "string", "description": "Search text for action='search'"},
                    "title": {"type": "string", "description": "Note title (for add/update)"},
                    "content": {"type": "string", "description": "Freeform body text. Use this for note_type='note'. Do NOT use this for checklists — pass `checklist_items` instead."},
                    "note_type": {"type": "string", "enum": ["note", "checklist"],
                                  "description": "'note' = freeform text in `content`. 'checklist' = to-do items in `checklist_items`. Defaults to checklist when checklist_items is given, else note."},
                    "checklist_items": {"type": "array",
                                        "items": {"type": "object",
                                                  "properties": {
                                                      "text": {"type": "string", "description": "The to-do item text"},
                                                      "done": {"type": "boolean", "description": "Whether the item is checked off"}
                                                  },
                                                  "required": ["text"]},
                                        "description": "Checklist items for note_type='checklist'. Each item is {text, done}. REQUIRED for checklists — leaving this empty produces a blank note."},
                    "color": {"type": "string", "description": "Optional color label (e.g. 'yellow', 'blue', 'green')"},
                    "label": {"type": "string", "description": "Optional category label (also used as a list filter)"},
                    "pinned": {"type": "boolean", "description": "Pin the note to the top"},
                    "archived": {"type": "boolean", "description": "For update: archive/unarchive. For list: show archived notes when true."},
                    "due_date": {"type": "string", "description": "Reminder time. Accepts natural language ('tomorrow at 9am', '11pm today') or ISO 8601. Fires a notification at that time."},
                    "index": {"type": "integer", "description": "Checklist item index (for toggle_item, 0-based)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_wellbeing",
            "description": "The user's private Lotus wellbeing data (mood and energy check-ins). patterns for planning a day or week, summary for 'how have I been lately', latest for the newest check-in (its note stays private), preferences for reminder setup; aggregates carry sample sizes. log_checkin only when the user asks to record how they feel. No diagnosis. Needs the endpoint scope enabled in Settings > Privacy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["summary", "patterns", "latest", "preferences", "log_checkin"],
                               "description": "The action to perform"},
                    "days": {"type": "integer", "description": "Window in days for summary/patterns (default 30, max 365)"},
                    "emotion_label": {"type": "string", "description": "For log_checkin: the word the user used, e.g. 'tired', 'calm'"},
                    "emotion_family": {"type": "string",
                                       "enum": ["pleasant_high", "pleasant_low", "unpleasant_high", "unpleasant_low"],
                                       "description": "For log_checkin: pleasantness x energy quadrant"},
                    "valence": {"type": "number", "description": "For log_checkin: pleasantness from -1 to 1 (optional; defaults from the family)"},
                    "energy": {"type": "number", "description": "For log_checkin: energy from 0 to 1 (optional; defaults from the family)"},
                    "intensity": {"type": "number", "description": "For log_checkin: strength from 0 to 1 (optional, default 0.5)"},
                    "note": {"type": "string", "description": "For log_checkin: the user's own words, stored privately. Never echoed back by any read action."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "For log_checkin: short context tags such as 'work', 'sleep'"},
                    "occurred_at": {"type": "string", "description": "For log_checkin: ISO timestamp with offset. Defaults to now."},
                    "timezone": {"type": "string", "description": "For log_checkin: IANA timezone name of the check-in"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "api_call",
            "description": "Call a registered API integration (RSS reader, git forge, bookmark manager, smart home, etc.). Check the system context for available integrations and their endpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "integration": {"type": "string", "description": "Integration name or ID (e.g. 'Miniflux', 'Gitea')"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method"},
                    "path": {"type": "string", "description": "API endpoint path (e.g. '/v1/entries?status=unread&limit=20')"},
                    "body": {"type": "object", "description": "JSON request body (for POST/PUT/PATCH)"}
                },
                "required": ["integration", "method", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_teacher",
            "description": "Ask a more capable AI model for help when stuck on a difficult problem. The teacher provides guidance that can be saved as a learned skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Teacher model name (e.g. 'claude-sonnet-4') or 'auto' for configured default"},
                    "problem": {"type": "string", "description": "Describe the problem or question you need help with"}
                },
                "required": ["problem"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_skills",
            "description": (
                "Skills: view full SKILL.md procedures (names=[...] loads several in one call; the skills index is already in your context, so list is rarely needed), or save a new or changed skill: add, edit (full content), patch (old_string to new_string), publish once the procedure has worked. Report the name the tool returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "view", "view_ref", "add", "edit", "patch", "publish", "delete", "search"], "description": "view | view_ref | search | add | edit | patch | publish | list | delete. add needs a kebab-case name."},
                    "name": {"type": "string", "description": "Skill slug. Required except for list and search (view also takes names). For add, use kebab-case and report the returned name."},
                    "names": {"type": "array", "items": {"type": "string"}, "description": "For view: several skill names to load in one call (preferred over repeated single-name views)."},
                    "path": {"type": "string", "description": "Sub-path under the skill directory, for view_ref."},
                    "description": {"type": "string", "description": "One-line summary for the skills index."},
                    "category": {"type": "string", "description": "Grouping such as 'dev', 'email', 'system'."},
                    "when_to_use": {"type": "string", "description": "Trigger conditions in plain English."},
                    "procedure": {"type": "array", "items": {"type": "string"}, "description": "Numbered steps."},
                    "pitfalls": {"type": "array", "items": {"type": "string"}, "description": "Known failure modes and recovery."},
                    "verification": {"type": "array", "items": {"type": "string"}, "description": "How to confirm the procedure worked."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Keyword tags."},
                    "platforms": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these OSes."},
                    "requires_toolsets": {"type": "array", "items": {"type": "string"}, "description": "Hide unless these toolsets are active."},
                    "fallback_for_toolsets": {"type": "array", "items": {"type": "string"}, "description": "Hide when these toolsets are active."},
                    "status": {"type": "string", "enum": ["draft", "published"], "description": "Defaults to 'draft' on add."},
                    "version": {"type": "string", "description": "Semver-ish, e.g. '1.0.0'."},
                    "confidence": {"type": "number", "description": "0-1 (for add/publish)."},
                    "content": {"type": "string", "description": "Full SKILL.md text (for edit)."},
                    "old_string": {"type": "string", "description": "Exact substring to replace (for patch). Must appear exactly once."},
                    "new_string": {"type": "string", "description": "Replacement text (for patch)."},
                    "query": {"type": "string", "description": "Search query (for search)."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_endpoints",
            "description": "Manage model API endpoints: list configured endpoints, add new ones, delete, enable or disable them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable"]},
                    "endpoint_id": {"type": "string", "description": "Endpoint ID (for delete/enable/disable)"},
                    "name": {"type": "string", "description": "Display name (for add)"},
                    "base_url": {"type": "string", "description": "API base URL e.g. https://api.openai.com/v1 (for add)"},
                    "api_key": {"type": "string", "description": "API key (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_mcp",
            "description": "Manage MCP (Model Context Protocol) tool servers: list servers and their tools, add new servers, delete, enable/disable, reconnect, or list all available tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable", "reconnect", "list_tools"]},
                    "server_id": {"type": "string", "description": "Server ID (for delete/enable/disable/reconnect)"},
                    "name": {"type": "string", "description": "Server name (for add)"},
                    "command": {"type": "string", "description": "Command to run e.g. npx (for add)"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments (for add)"},
                    "env": {"type": "object", "description": "Environment variables (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_webhooks",
            "description": "Manage webhooks: list, add, delete, enable or disable webhook endpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable"]},
                    "webhook_id": {"type": "string", "description": "Webhook ID (for delete/enable/disable)"},
                    "name": {"type": "string", "description": "Webhook name (for add)"},
                    "url": {"type": "string", "description": "Webhook URL (for add)"},
                    "events": {"type": "string", "description": "Comma-separated event names (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tokens",
            "description": "Manage API access tokens: list existing tokens, create new ones, or delete them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "delete"]},
                    "token_id": {"type": "string", "description": "Token ID (for delete)"},
                    "name": {"type": "string", "description": "Token name (for create)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_documents",
            "description": "Manage documents: list all documents (with optional search/language filter), delete documents, or run tidy cleanup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "delete", "tidy"]},
                    "document_id": {"type": "string", "description": "Document ID (for delete)"},
                    "search": {"type": "string", "description": "Search query (for list)"},
                    "language": {"type": "string", "description": "Filter by language (for list)"},
                    "limit": {"type": "integer", "description": "Max results (for list, default 50)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_settings",
            "description": "Manage user preferences and settings. Use `disable_tool`/`enable_tool`/`list_tools` to turn individual tools on or off globally (e.g. shell, search, browser, documents, memory, skills, images, tasks, notes, calendar, email). Use list/get/set/delete for free-form preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "set", "delete", "disable_tool", "enable_tool", "list_tools"]},
                    "key": {"type": "string", "description": "Setting key (for get/set/delete)"},
                    "value": {"description": "Setting value (for set) — can be string, number, boolean, or object"},
                    "tool": {"type": "string", "description": "Tool to turn off or on: an alias (shell, search, browser, documents, memory, skills, images, tasks, notes, calendar, email) or a raw name like 'bash'."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download_model",
            "description": "Download a HuggingFace model to a server. If `host` is omitted, defaults to the cookbook's currently-selected server (NOT localhost) — call list_cookbook_servers first if you're unsure where it should go.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "HuggingFace repo (e.g. 'Qwen/Qwen3-8B')"},
                    "host": {"type": "string", "description": "Cookbook server NAME from list_cookbook_servers (e.g. 'gpu-box') or user@host. Omit for the cookbook's selected default."},
                    "local": {"type": "boolean", "description": "Force download to THIS machine (localhost) instead of the default remote server."},
                    "include": {"type": "string", "description": "Glob filter for specific files (e.g. '*Q4_K_M*')"},
                },
                "required": ["repo_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "serve_model",
            "description": "Start serving a model (vLLM, SGLang, llama.cpp, Ollama, MLX Image, Diffusers). Omitted `host` means the cookbook's selected server, not localhost. Image models use scripts/mlx_image_server.py (Apple Silicon) or scripts/diffusion_server.py (other), never mlx_lm.server. Then call list_served_models for readiness; on a failure it returns a diagnosis, and you retry serve_model with its adjusted cmd.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "Model repo (e.g. 'Qwen/Qwen3-8B')"},
                    "cmd": {"type": "string", "description": "Full serve command, e.g. 'vllm serve <repo> --port 8000 --tp 2' or 'python3 scripts/diffusion_server.py --model <repo> --port 8100'"},
                    "host": {"type": "string", "description": "Target server — friendly NAME from list_cookbook_servers (e.g. 'gpu-box', 'workstation') or raw user@host. Omit to use the cookbook's selected default."},
                    "local": {"type": "boolean", "description": "Force serve on THIS machine instead of the default remote server."},
                },
                "required": ["repo_id", "cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_served_models",
            "description": "List currently running model servers with status, model name, port, throughput, and structured Cookbook diagnoses. If a serve failed, this includes recent logs plus retry suggestions/adjusted commands the agent can use with serve_model.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stop_served_model",
            "description": "Stop a running model server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session ID of the server to stop"},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tail_serve_output",
            "description": "Read the last lines of a cookbook serve or download task's tmux pane. Use it after you launched a model with serve_model and list_served_models reports that NEW task as crashed or errored; read the root cause, then call serve_model again with adjusted flags. Old stopped tasks are history and say nothing about the current attempt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session id from list_served_models (e.g. 'serve-abc12345', 'cookbook-a1b2c3d4')."},
                    "tail": {"type": "integer", "description": "Lines of scrollback to fetch (default 300, max 4000). Raise it when the error points to an earlier line."},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_downloads",
            "description": "List in-progress model downloads in the Cookbook. Shows each download's model name, phase, percent (if available), session ID, and remote host.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_download",
            "description": "Cancel an in-progress model download by killing its tmux session. Use list_downloads first to get the session_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session ID from list_downloads (e.g. 'cookbook-a1b2c3d4')"},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_hf_models",
            "description": "Search HuggingFace for models matching a query. Returns a ranked list of repo IDs, sizes (when available), and download counts. Use this when the user wants to find a model to download.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms (e.g. 'Qwen 8B', 'flux', 'llama-3 instruct')"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_cookbook_servers",
            "description": "List the cookbook's configured servers (remote GPU boxes + local) and the current default host. Call this before download_model/serve_model when the user didn't specify a host, so models go to the right machine (where the GPUs and model cache are) instead of localhost. If multiple servers and intent is ambiguous, show them and ask the user which.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_serve_presets",
            "description": "List saved Cookbook serve presets. Each preset is a launch template (name, model, host, port, tmux cmd) the user previously saved from the UI. Call this BEFORE raw serve_model when the user asks to launch a model by name manually.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "adopt_served_model",
            "description": "Register an existing tmux model server (started manually or outside the cookbook flow) into Cookbook tracking, AND add it as a chat endpoint. Use when the user (or you) launched something via ssh+tmux and now want it visible in the UI / stoppable via stop_served_model / usable in the model picker. Verifies the tmux session + port respond before adding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Remote host in user@host form (e.g. 'user@192.0.2.10'). Omit for localhost."},
                    "tmux_session": {"type": "string", "description": "Existing tmux session name (e.g. 'minimax-m27')"},
                    "model": {"type": "string", "description": "Model repo_id or display name (e.g. 'cyankiwi/MiniMax-M2.7-AWQ-4bit')"},
                    "port": {"type": "integer", "description": "Port the server is listening on (default 8000)"},
                    "name": {"type": "string", "description": "Optional display name (defaults to model basename)"},
                    "add_endpoint": {"type": "boolean", "description": "Also register as a chat endpoint (default true)"}
                },
                "required": ["tmux_session", "model"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "serve_preset",
            "description": "Launch a saved Cookbook serve preset by name. Reuses the exact tmux command + host the user saved before. This is the preferred way to start a known model (SD3.5, vLLM presets, etc.) — don't fabricate launch commands when a preset exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Preset name (exact or case-insensitive substring of one returned by list_serve_presets)"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_cached_models",
            "description": "List models already cached on disk locally or on a remote server. `host` accepts friendly Cookbook server names from list_cookbook_servers (for example workstation) or raw user@host. Also reports completed Cookbook download tasks when the filesystem cache scan cannot locate the HF cache path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Friendly Cookbook server name (e.g. 'workstation', 'gpu-box') or raw remote host (e.g. 'user@gpu-box'). Omit for local."},
                    "model_dir": {"type": "string", "description": "Comma-separated additional model directories to scan beyond ~/.cache/huggingface/hub"},
                    "ssh_port": {"type": "string", "description": "SSH port for remote host (default 22)"},
                    "platform": {"type": "string", "enum": ["linux", "windows"], "description": "Remote platform"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "app_api",
            "description": "Loopback to the internal Odysseus endpoints the UI uses (cookbook, gallery, library, memory, notes, calendar, tasks, settings, themes, research), for what no named tool covers. action='endpoints' pages the OpenAPI list (filter, then limit/offset); action='call' (default) takes method, path, body. Auth, user, admin, shell, install and email-account paths are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["call", "endpoints"], "description": "'call' to hit an endpoint, 'endpoints' to list what's available"},
                    "path": {"type": "string", "description": "Endpoint path starting with /api/ (e.g. '/api/cookbook/gpus', '/api/gallery/list', '/api/calendar/events')"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method (default GET)"},
                    "body": {"type": "object", "description": "JSON request body for POST/PUT/PATCH"},
                    "query": {"type": "object", "description": "Querystring params as a key-value object"},
                    "filter": {"type": "string", "description": "For action=endpoints: substring to filter paths/summaries (e.g. 'cookbook', 'gallery'); filter before paging"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 25, "description": "For action=endpoints: results per compact page (default 25, max 50)"},
                    "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "For action=endpoints: zero-based offset after filtering"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_agent_loadout",
            "description": "Workers: start launches one detached worker chat per task, status shows what it did (wait_seconds blocks until it finishes), stop ends it. Also manages reusable loadouts (named policies, limited to this chat's own). A finished worker's result is handed back here and kept in result_ref, readable with recall_tool_output. The worker has not seen this conversation, so `task` carries the full brief.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "capabilities", "preflight", "create", "update", "delete", "start", "status", "stop", "export", "import"], "description": "start (needs task) | status | stop | list | get | capabilities | preflight | create | update | delete | export | import. Default list."},
                    "detail": {"type": "boolean", "description": "capabilities only: include the complete allowed tool-name list."},
                    "name": {"type": "string", "description": "Loadout name (1-40 chars). Required for get/create/update/delete; optional for start."},
                    "task": {"type": "string", "description": "Required for start: the whole assignment. The worker has not seen this conversation."},
                    "description": {"type": "string", "description": "One line explaining when to use this loadout."},
                    "instructions": {"type": "string", "description": "System instructions the worker starts with. Narrowing a loadout or rewording it needs no approval; widening its access does."},
                    "persona_name": {"type": "string", "description": "Name the worker answers as."},
                    "temperature": {"type": "number", "description": "0-2 sampling temperature for this loadout. Omit for the app default."},
                    "max_tokens": {"type": "integer", "description": "Reply length cap for this loadout (0 = server decides). Omit for the app default."},
                    "model": {"type": "string", "description": "Model for the worker. Omit to inherit."},
                    "model_fallbacks": {"type": "array", "items": {"type": "string"}},
                    "model_access": {"type": "string", "enum": ["current", "selected", "all"], "description": "Whether the worker may switch model."},
                    "allowed_models": {"type": "array", "items": {"type": "string"}},
                    "tool_access": {"type": "string", "enum": ["all", "selected", "none"], "description": "create/update: 'selected' with enabled_tools; 'all' is refused. Omitted = the read-only set."},
                    "enabled_tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names allowed when tool_access=selected; '@read_only', mcp__<server>__<tool> and mcp__<server>__* work. Else denied."},
                    "disabled_tools": {"type": "array", "items": {"type": "string"}, "description": "Extra tools to deny on top of tool_access."},
                    "required_tools": {"type": "array", "items": {"type": "string"}, "description": "create/update: tools the mission needs; if any is denied or unknown, nothing is saved."},
                    "memory_access": {"type": "string", "enum": ["none", "read", "write"]},
                    "skill_access": {"type": "string", "enum": ["all", "selected", "none"]},
                    "skill_names": {"type": "array", "items": {"type": "string"}},
                    "mcp_access": {"type": "string", "enum": ["all", "selected", "none"]},
                    "allowed_mcp_servers": {"type": "array", "items": {"type": "string"}},
                    "private_vault_access": {"type": "boolean", "description": "Private vault notes. Granted only if this chat has it."},
                    "shell_access": {"type": "string", "enum": ["sandbox", "host", "off"], "description": "How bash/python run: sandbox (default), host (only if this chat has it), off."},
                    "approval_mode": {"type": "string", "enum": ["inherit", "auto", "ask_risky", "ask_all"], "description": "Never looser than this chat's own mode."},
                    "delegation_policy": {"type": "string", "enum": ["never", "explicit", "auto"]},
                    "max_parallel_workers": {"type": "integer", "description": "0-8, capped at this chat's own limit."},
                    "max_rounds": {"type": "integer", "description": "Round at which the worker wraps up and hands back what it has (0 = no budget; max 200)."},
                    "parent_session": {"type": "string", "description": "start only: leave unset."},
                    "run_id": {"type": "string", "description": "stop/status: the worker run (from start/status). Omit when exactly one runs."},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "description": "status only: block up to this long for the watched run to finish."},
                    "worker_session": {"type": "string", "description": "stop only: the worker's chat id, instead of run_id."},
                    "workspace": {"type": "string", "description": "start only: the checkout the worker's file tools work in (a path from get_workspace). Required when this chat has none and the task touches a repository."},
                    "requires": {"type": "array", "items": {"type": "string", "enum": ["workspace", "write", "read_only", "no_workspace"]}, "description": "start only: what the task needs; an unmet need refuses the start, with the fix."},
                    "extra_tools": {"type": "array", "items": {"type": "string"}, "description": "start only: tools this ONE worker gets beyond its loadout (within this chat's policy)."},
                    "parallel": {"type": "boolean", "description": "start only: true to start a loadout already working in another chat."},
                    "clear": {"type": "array", "items": {"type": "string"}, "description": "update only: field names to reset to their default."},
                    "names": {"type": "array", "items": {"type": "string"}, "description": "export only: loadouts to export. Omit for all."},
                    "document": {"type": "object", "description": "import only: the JSON document an export returned."},
                    "mode": {"type": "string", "enum": ["merge", "replace"], "description": "import only: merge (default) or replace the whole list."},
                    "rename_conflicts": {"type": "boolean", "description": "import merge only: rename a same-named loadout instead of overwriting."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "orchestrate_agents",
            "description": "Run a research workflow: scoped read-only specialist agents, then an optional synthesis agent. A small task is one specialist and no synthesis. start returns a preflight row per agent; a blocked agent cannot do its branch, so never report that branch as researched. wait and status return the handoffs and a structured `record`. Report completion only after status=completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "status", "wait", "cancel", "resume"], "description": "resume starts a new workflow from a finished incomplete one, reusing completed handoffs and launching only synthesis (or the named stages/retry_children)."},
                    "task": {"type": "string", "description": "Overall objective, deliverables and constraints; start only."},
                    "specialists": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                        "type": "object", "properties": {
                            "name": {"type": "string"}, "task": {"type": "string"},
                            "tools": {"type": "array", "items": {"type": "string"}, "description": "Exact read-only tool names (web_search, read_file, grep, ...) or mcp__server__tool. A write tool fails the start; the rejection lists the full supported set."},
                            "skills": {"type": "array", "items": {"type": "string"}},
                            "model": {"type": "string", "description": "Optional exact configured model ID, for example gpt-5.6-luna. Omit to inherit the parent model; do not send 'default' or a display label."},
                            "required": {"type": "boolean", "description": "Whether synthesis must wait for a completed, evidence-backed result from this branch. Defaults to true."},
                            "max_rounds": {"type": "integer", "minimum": 0, "maximum": 200, "description": "Advisory round budget for this specialist (0 = unlimited; at most 200). It does not stop a run: the tool-call limit, stall detection and timeouts do."}
                        }, "required": ["name", "task", "tools"]
                    }},
                    "synthesis": {"type": "object", "description": "Optional synthesis agent; gets the specialists' handoffs, failures included. State the exact output required.", "properties": {
                        "name": {"type": "string"}, "task": {"type": "string"},
                        "model": {"type": "string", "description": "Optional exact configured model ID; omit to inherit."}, "skills": {"type": "array", "items": {"type": "string"}}
                    }},
                    "workflow_id": {"type": "string", "description": "Returned by start; required for status/wait/cancel."},
                    "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 1800},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60},
                    "allow_partial_synthesis": {"type": "boolean", "description": "start only. Defaults false. When true, synthesis may run with missing required branches but must label its result provisional and identify the gaps."},
                    "retries": {"type": "integer", "minimum": 0, "maximum": 1, "description": "Optional bounded retry of failed read-only specialists; default 0."},
                    "persist_document": {"description": "start/resume: true or {title}. Saves the final result as one editor document owned by this chat; the record lists it in editor_documents_created.", "anyOf": [{"type": "boolean"}, {"type": "object", "properties": {"title": {"type": "string"}}}]},
                    "stages": {"type": "array", "items": {"type": "string", "enum": ["synthesis", "research"]}, "description": "resume only. Which stages launch again; default [synthesis]. research relaunches only branches that did not complete."},
                    "retry_children": {"type": "array", "items": {"type": "string"}, "description": "resume only. Exact agent names to launch again even if they completed."}
                }, "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_agent",
            "description": "Hand a bounded coding task to the administrator-selected provider (the local Claude Code CLI or a connected remote coding-agent MCP tool; authentication and billing stay with it). Actions: status, list_repositories, run, start, poll, cancel, list, as the provider supports. For repository-wide audits or multi-file work use action=start, then poll with wait_seconds; run is for short tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["run", "start", "poll", "cancel", "list", "status", "list_repositories"]},
                    "repository": {"type": "string", "description": "Repository or worktree understood by the selected provider."},
                    "prompt": {"type": "string", "description": "Bounded coding task instructions."},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer"},
                    "model": {"type": "string", "description": "Optional, provider-specific; omit for its default. Claude Code takes Claude aliases (opus, sonnet) or IDs such as claude-opus-5-5."},
                    "task_id": {"type": "string"},
                    "wait_seconds": {"type": "integer"},
                    "label": {"type": "string"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_claude_code",
            "description": "Claude Code CLI as a coding agent for a bounded task in an approved repository: it inspects, edits, tests and commits, never pushes. Call action=status first when unsure it is installed. action=start returns a task_id to poll with wait_seconds (use it for repository-wide audits and multi-file work); run waits for short jobs. A GitHub owner/repo goes to the cloud runner and returns a draft PR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["run", "start", "poll", "cancel", "list", "status", "list_repositories", "update"], "description": "Default run. status = preflight; list_repositories; start/poll/cancel/list = background tasks; update = fix claude_code_outdated (admin)."},
                    "repository": {"type": "string", "description": "Absolute path from list_repositories, or an allowlisted GitHub owner/repo for the cloud runner. Omit for the default checkout."},
                    "via": {"type": "string", "enum": ["local", "cloud"], "description": "local = this container; cloud = GitHub Actions on an allowlisted repository. Omit to decide from the repository."},
                    "base_branch": {"type": "string", "description": "cloud only: branch to start from (default: the repository's default branch)."},
                    "prompt": {"type": "string", "description": "Task instructions for Claude Code (run/start). State the objective, likely files, constraints, and how to verify."},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Narrower permission list; omit for defaults. Accepted: Read, Glob, Grep, Edit, Write, Bash(git ...), Bash(<test runner>:*)."},
                    "timeout_seconds": {"type": "integer", "description": "Maximum runtime, 30-1800 seconds (default 900)."},
                    "model": {"type": "string", "description": "A Claude alias (opus, sonnet, haiku, fable) or ID such as claude-opus-5-5; omit for the default. gpt-*, o3 and gemini are rejected."},
                    "task_id": {"type": "string", "description": "Task id for poll/cancel."},
                    "wait_seconds": {"type": "integer", "description": "poll only: block up to this many seconds (max 600) for the task to finish. Use this rather than bash sleep."},
                    "label": {"type": "string", "description": "Short name for a background task (start)."},
                    "version": {"type": "string", "description": "update only: latest, stable or an exact version. Omit for the default."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": "Edit a gallery image: upscale, remove background, inpaint, or harmonize.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string", "description": "Gallery image ID"},
                    "action": {"type": "string", "enum": ["upscale", "rembg", "inpaint", "harmonize"], "description": "Edit action"},
                    "prompt": {"type": "string", "description": "For inpaint: what to fill the masked area with"},
                    "scale": {"type": "number", "description": "For upscale: scale factor (default 2)"},
                },
                "required": ["image_id", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_research",
            "description": "Start a deep research task on a topic. Returns a task ID for tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Research question or topic"},
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_contact",
            "description": "Look up a contact by name. Searches CardDAV address book and sent email history. Returns email addresses (when available) or phone numbers. Use when the user says 'message [name]', 'email [name]', or asks for someone's contact details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Person's name to search for"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_contact",
            "description": "Create, update, delete, or list the user's CardDAV contacts. Use to save a new contact, update an existing one (email/phone/address), or remove one. Add does not require email: name + phone or name + address is valid. For update/delete you need the contact's uid — call action='list' first to find it. Writes go through the same dedupe + validation as the Contacts UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "update", "delete"],
                               "description": "list = show all contacts (with uids); add = create; update = edit by uid; delete = remove by uid."},
                    "uid": {"type": "string", "description": "Contact UID (required for update/delete; get it from action=list)."},
                    "name": {"type": "string", "description": "Contact's display name (for add/update)."},
                    "email": {"type": "string", "description": "Single email address (convenience for add, or the primary email for update). Optional when phone or address is provided."},
                    "emails": {"type": "array", "items": {"type": "string"}, "description": "Full list of email addresses (first is primary)."},
                    "phones": {"type": "array", "items": {"type": "string"}, "description": "Full list of phone numbers. Valid for add/update."},
                    "address": {"type": "string", "description": "Postal/mailing address as a single human-readable string."},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_email_accounts",
            "description": "List configured email accounts. Use this before checking mail when the user names a mailbox/account such as Gmail, work, or a custom domain, then pass the returned account name/email/id to the other email tools.",
            "parameters": {
                "type": "object",
                "properties": {},
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send a new email. Use resolve_contact first if you only have a name and need to find the email address. If multiple accounts exist, pass account from list_email_accounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
                "required": ["to", "subject", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_emails",
            "description": "List emails from an account/folder, newest first. Returns subject, sender, date, UID, and account for each email. Use list_email_accounts first when the user mentions Gmail/work/a custom mailbox. For last/latest/newest email requests, use max_results=1 and unread_only=false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "max_results": {"type": "integer", "description": "Max emails to return (default: 20)"},
                    "limit": {"type": "integer", "description": "Backward-compatible alias for max_results"},
                    "unread_only": {"type": "boolean", "description": "Only show unread emails. Default false; set true only when the user asks for unread emails."},
                    "unresponded_only": {"type": "boolean", "description": "Only show unanswered emails. Default false."},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the full content of a specific email by UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID to read"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, especially when the UID came from a non-default mailbox"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scan_email_unsubscribes",
            "description": "Scan recent email headers for likely spam/newsletter unsubscribe candidates. Does not unsubscribe anything. Review candidates with the user before acting; mailto methods can be executed with unsubscribe_email, web URL methods require browser/web tools after approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder to scan (default: INBOX)"},
                    "limit": {"type": "integer", "description": "Maximum candidates to return (default: 25)"},
                    "max_scan": {"type": "integer", "description": "How many newest emails to inspect (default: 150)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts"},
                },
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "audit_emails",
            "description": "Search and summarize a whole mailbox in one call: for 'go through my inbox and report on X'. The IMAP server runs the search, so it reaches all mail; pass `query` (Gmail syntax) and/or since/before, or keywords elsewhere. The result leads with a `summary` (counts by domain, month, keyword over every match); report totals from it, not from the capped digest rows. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder to scan (default: INBOX)"},
                    "query": {"type": "string", "description": "Gmail accounts only: raw Gmail search syntax (from:, subject:, OR, newer_than:, has:), run over the whole mailbox. Other accounts use keywords/since/before."},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Terms to match in subject or body. Used to build the server-side search where possible, applied as a client-side filter otherwise. Max 12."},
                    "since": {"type": "string", "description": "Only messages on/after this date, ISO YYYY-MM-DD. Runs server-side."},
                    "before": {"type": "string", "description": "Only messages before this date, ISO YYYY-MM-DD. Runs server-side."},
                    "summarize": {"type": "boolean", "description": "Return the aggregate summary plus a small sample digest instead of a full digest. Use for broad report/counting sweeps."},
                    "limit": {"type": "integer", "description": "Maximum digest entries to return (default: 30, max 100)"},
                    "max_scan": {"type": "integer", "description": "How many matched messages to inspect (default: 80). Up to 2000 when query/keywords/dates narrowed the set server-side, 250 for an unfiltered scan."},
                    "snippet_chars": {"type": "integer", "description": "Max characters of body snippet per returned message (default: 300); 0 skips body fetching entirely for a counts-only pass"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts"},
                },
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "unsubscribe_email",
            "description": "Execute one approved unsubscribe action for an email UID. Safe mailto List-Unsubscribe methods are sent/staged. Web URL methods return a requires-browser instruction and exact URL; use browser/web tools only after user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from scan_email_unsubscribes/list_emails"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "method_index": {"type": "integer", "description": "Method index from scan_email_unsubscribes (default: 0)"},
                    "allow_web": {"type": "boolean", "description": "Return browser/web instructions when selected method is URL"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reply_to_email",
            "description": "SEND a reply email immediately by UID. Do not use this when the user asks to write/draft/open/start a reply; use ui_control action=open_email_reply with body instead so the user can review. Only use when the user explicitly says to send now. Use the exact UID from the latest read_email/list_emails result; never invent UID 1. Automatically threads with In-Reply-To/References headers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Exact UID of the email to reply to from list_emails/read_email; never invent UID 1"},
                    "body": {"type": "string", "description": "Reply body text"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, especially when the UID came from a non-default mailbox"},
                },
                "required": ["uid", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_email",
            "description": "Perform one action on many emails at once. Use this for 'delete all those', 'archive these', 'mark all read', or any bulk operation after list_emails. Always pass account when the listed emails came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["mark_read", "mark_unread", "archive", "delete", "junk"], "description": "Bulk action to perform"},
                    "uids": {"type": "array", "items": {"type": "string"}, "description": "UIDs from the latest list_emails result"},
                    "all_unread": {"type": "boolean", "description": "Operate on all unread messages in folder instead of explicit UIDs"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "permanent": {"type": "boolean", "description": "For delete: hard-delete instead of moving to Trash"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_email",
            "description": "Delete one email by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "permanent": {"type": "boolean", "description": "Hard-delete instead of moving to Trash"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "archive_email",
            "description": "Archive one email by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_email_read",
            "description": "Mark one email as read or unread by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "read": {"type": "boolean", "description": "True marks read; false marks unread"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "message_agent",
            "description": "Tell another RUNNING agent something now, without waiting for a reply (send_to_session blocks until the target answers). The message lands before the target's next round, tagged as coming from you. Use it to steer, warn or hand a status to a peer doing independent work; to delegate a task and wait for the outcome, use send_to_session. A few sends per turn, to sessions you own.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The id of the running agent session to message"},
                    "message": {"type": "string", "description": "The message to deliver now"}
                },
                "required": ["session_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_bg_jobs",
            "description": "Inspect and control detached background `bash` jobs (started with the `#!bg` marker). action='list' shows this chat's jobs with id/status/age/command; action='output' returns a job's captured output so far (use for a still-running job, or to re-read a finished one); action='kill' terminates a runaway job's process tree instead of waiting out its max-runtime. output and kill need job_id from list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "output", "kill"], "description": "list | output | kill (default: list)"},
                    "job_id": {"type": "string", "description": "Background job id (required for output/kill; from action='list')"},
                },
                "required": ["action"]
            }
        }
    },
    # Appended last on purpose: additive at the tail of the list keeps this
    # entry out of the way of concurrent edits elsewhere in the file.
    {
        "type": "function",
        "function": {
            "name": "inspect_runtime",
            "description": (
                "Explain why a scheduled task behaved as it did and where a setting's value came from. action='tasks' lists your tasks with their scheduler lane. action='task' with task_id returns recent runs: the prompt actually sent, outcome (and what aborted it), tool calls, policy. action='config' names a setting's source (env, settings.json, preferences, code default). Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["tasks", "task", "config"],
                        "description": "tasks | task | config (default: tasks)",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task id, required for action='task' (from action='tasks').",
                    },
                    "runs": {
                        "type": "integer",
                        "description": "How many recent runs to report for action='task' (default 5, max 20).",
                    },
                    "include_traces": {
                        "type": "boolean",
                        "description": "Include per-run tool-call traces (default true).",
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Setting keys for action='config'. Omit for every key whose value is not the code default.",
                    },
                },
                "required": ["action"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Converter: native function call -> ToolBlock
# ---------------------------------------------------------------------------

def _decode_loose_json_string(value: str) -> str:
    """Decode common JSON string escapes without requiring inner quotes to be escaped."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\" or i + 1 >= len(value):
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "b":
            out.append("\b")
        elif nxt == "f":
            out.append("\f")
        elif nxt in ('"', "\\", "/"):
            out.append(nxt)
        elif nxt == "u" and i + 5 < len(value):
            try:
                out.append(chr(int(value[i + 2:i + 6], 16)))
                i += 4
            except ValueError:
                out.append("\\" + nxt)
        else:
            out.append("\\" + nxt)
        i += 2
    return "".join(out)


def _repair_document_function_args(tool_type: str, arguments: str) -> Optional[dict]:
    """Salvage obvious malformed document tool args from local model wrappers.

    The doc LoRA sometimes emits the right native tool call but puts raw quotes
    inside the document text, making the surrounding JSON invalid. Treat that as
    a wrapper parse failure, not a semantic tool-choice failure.
    """
    if tool_type != "update_document" or not isinstance(arguments, str):
        return None
    raw = arguments.strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        return None
    for key in ("content", "conten"):
        marker = f'"{key}"'
        key_pos = raw.find(marker)
        if key_pos < 0:
            continue
        colon_pos = raw.find(":", key_pos + len(marker))
        if colon_pos < 0:
            continue
        first_quote = raw.find('"', colon_pos + 1)
        if first_quote < 0:
            continue
        close_brace = raw.rfind("}")
        last_quote = raw.rfind('"', first_quote + 1, close_brace)
        if last_quote <= first_quote:
            continue
        content = _decode_loose_json_string(raw[first_quote + 1:last_quote])
        return {"content": content}
    return None


def _with_document_target(content: str, args: dict) -> str:
    """Carry an explicit edit/update/suggest_document target in the tool content.

    ToolBlock is (tool_type, content), so the optional `document_id` argument
    rides as a `<<<DOCUMENT_ID: ...>>>` first line the document tools strip.
    """
    ref = args.get("document_id") or args.get("doc_id") or args.get("document")
    if not isinstance(ref, (str, int)) or not str(ref).strip():
        return content
    from src.agent_tools.document_tools import with_document_id_header
    return with_document_id_header(content, ref)


def function_call_to_tool_block(name: str, arguments: str) -> Optional[ToolBlock]:
    """Convert a native function call into a ToolBlock for the existing execution pipeline."""
    tool_type = _TOOL_NAME_MAP.get(name, name)
    try:
        if not arguments or (isinstance(arguments, str) and not arguments.strip()):
            args = {}
        else:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (json.JSONDecodeError, TypeError):
        args = _repair_document_function_args(tool_type, arguments)
        if args is not None:
            logger.warning(f"Repaired malformed document function call arguments for {name}")
        else:
            logger.error(f"Failed to parse function call arguments for {name}: {arguments}")
            return None

    # Some models emit valid JSON that isn't an object (e.g. a bare array
    # ["ls -la"], string, or number) as function arguments. Most local tools keep
    # the legacy empty-object coercion for stream robustness, but email MCP tools
    # must fail closed so a malformed call cannot read the default mailbox.
    # Uses the shared BUILTIN_EMAIL_TOOLS (single source of truth) so the
    # fail-closed set can't drift from the dispatch/blocklist sets.
    if not isinstance(args, dict):
        if tool_type.startswith("mcp__email__") or name in BUILTIN_EMAIL_TOOLS:
            logger.warning(f"Non-object email function call arguments for {name}: {args!r}; rejecting")
            return None
        logger.warning(f"Non-object function call arguments for {name}: {args!r}; treating as empty")
        args = {}

    required_args = _REQUIRED_NATIVE_TOOL_ARGS.get(tool_type)
    if required_args and not any(str(args.get(key) or "").strip() for key in required_args):
        logger.warning(f"Rejecting empty required arguments for function call {name}: {args!r}")
        return None

    # Allow MCP tools through (namespaced as mcp__serverid__toolname)
    if tool_type.startswith("mcp__"):
        content = json.dumps(args) if args else "{}"
        return ToolBlock(tool_type, content)
    # Email tools are implemented as MCP — route them to email
    if name in BUILTIN_EMAIL_TOOLS:
        return ToolBlock(f"mcp__email__{name}", json.dumps(args) if args else "{}")
    if tool_type not in TOOL_TAGS:
        logger.warning(f"Unknown function call: {name}")
        return None

    # Convert structured args back to the text format each tool expects
    if tool_type == "bash":
        content = args.get("command", "")
        if args.get("idle_timeout") not in (None, ""):
            from src.tool_types import ToolBlockWithOptions

            return ToolBlockWithOptions(tool_type, content, {"idle_timeout": args.get("idle_timeout")})
    elif tool_type == "python":
        content = args.get("code", "")
    elif tool_type == "web_search":
        queries = args.get("queries")
        if isinstance(queries, list) and queries:
            content = str(queries[0])
        elif queries:
            content = str(queries)
        else:
            content = args.get("query", "")
        # Preserve the model-requested freshness filter — the web_search schema
        # advertises time_filter and the executor parses {"query","time_filter"},
        # but a bare query string dropped it. Mirrors the read_file JSON idiom.
        tf = args.get("time_filter")
        if content and isinstance(tf, str) and tf in ("day", "week", "month", "year"):
            content = json.dumps({"query": content, "time_filter": tf})
    elif tool_type == "read_file":
        # Plain path (back-compat) unless a line range is requested → JSON.
        if args.get("offset") or args.get("limit"):
            content = json.dumps(args)
        else:
            content = args.get("path", "")
    elif tool_type in ("grep", "glob", "ls", "preview_file"):
        content = json.dumps(args) if args else "{}"
    elif tool_type == "get_workspace":
        content = ""
    elif tool_type == "write_file":
        content = args.get("path", "") + "\n" + args.get("content", "")
    elif tool_type == "edit_file":
        content = json.dumps(args)
    elif tool_type == "apply_patch":
        content = args.get("patch_text") or args.get("patchText") or args.get("patch") or ""
    elif tool_type == "todowrite":
        content = json.dumps(args)
    elif tool_type == "create_document":
        parts = [args.get("title", "Untitled")]
        if args.get("language"):
            parts.append(args["language"])
        parts.append(args.get("content", ""))
        content = "\n".join(parts)
    elif tool_type == "edit_document":
        blocks = []
        edits = args.get("edits", [])
        if not isinstance(edits, list):
            edits = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            blocks.append(
                f'<<<FIND>>>\n{edit.get("find", "")}\n<<<REPLACE>>>\n{edit.get("replace", "")}\n<<<END>>>'
            )
        content = _with_document_target("\n".join(blocks), args)
    elif tool_type == "suggest_document":
        blocks = []
        suggestions = args.get("suggestions", [])
        if not isinstance(suggestions, list):
            suggestions = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            blocks.append(
                f'<<<FIND>>>\n{s.get("find", "")}\n<<<SUGGEST>>>\n{s.get("replace", "")}\n<<<REASON>>>\n{s.get("reason", "")}\n<<<END>>>'
            )
        content = _with_document_target("\n".join(blocks), args)
    elif tool_type == "update_document":
        content = _with_document_target(args.get("content", ""), args)
    elif tool_type == "search_chats":
        content = args.get("query", "")
    elif tool_type == "chat_with_model":
        content = args.get("model", "") + "\n" + args.get("message", "")
    elif tool_type == "create_session":
        content = args.get("name", "Untitled") + "\n" + args.get("model", "")
    elif tool_type == "list_sessions":
        content = args.get("filter", "")
    elif tool_type == "send_to_session":
        if args.get("mode") or args.get("profile"):
            payload = {"session_id": args.get("session_id", ""), "message": args.get("message", ""),
                       "mode": args.get("mode") or "agent"}
            if args.get("profile"):
                payload["profile"] = args.get("profile")
            content = json.dumps(payload)
        else:
            content = args.get("session_id", "") + "\n" + args.get("message", "")
    elif tool_type == "message_agent":
        content = json.dumps({"session_id": args.get("session_id", ""), "message": args.get("message", "")})
    elif tool_type == "pipeline":
        # Pass as JSON for the pipeline parser
        content = json.dumps({"steps": args.get("steps", [])})
    elif tool_type == "manage_session":
        action = args.get("action", "")
        value = args.get("value", "")
        # `list` is the only action that takes an OPTIONAL keyword
        # filter — never a session_id. Don't leak the "current" default
        # into the filter slot (was producing "No sessions found
        # matching 'current'" when the agent omitted session_id).
        if action == "list":
            keyword = args.get("session_id", "") or args.get("keyword", "") or value
            content = "list" + (("\n" + keyword) if keyword and keyword.lower() != "current" else "")
        else:
            sid = args.get("session_id", "current")
            content = action + "\n" + sid
            if value:
                content += "\n" + value
    elif tool_type == "manage_memory":
        action = args.get("action", "")
        if action == "add":
            text = args.get("text") or args.get("value") or args.get("content") or ""
            if not text and args.get("key"):
                text = str(args.get("key") or "")
            content = "add\n" + str(text)
            if args.get("category"):
                content += "\n" + args["category"]
            elif args.get("key"):
                content += "\n" + str(args["key"])
        elif action == "edit":
            content = "edit\n" + args.get("memory_id", "") + "\n" + args.get("text", "")
        elif action == "delete":
            content = "delete\n" + args.get("memory_id", "")
        elif action == "search":
            content = "search\n" + (args.get("text") or args.get("tex") or args.get("query") or "")
        elif action == "list":
            content = "list"
            if args.get("category"):
                content += "\n" + args["category"]
        else:
            content = action
    elif tool_type == "list_models":
        content = args.get("filter", "")
    elif tool_type == "ui_control":
        action = args.get("action", "")
        name = args.get("name", "")
        value = args.get("value", "")
        if action == "toggle":
            content = f"toggle {name} {value}"
        elif action == "open_panel":
            content = f"open_panel {name or value}"
        elif action == "open_email_reply":
            uid = args.get("uid") or name
            folder = args.get("folder") or value or "INBOX"
            mode = args.get("mode") or "reply"
            content = f"open_email_reply {uid} {folder} {mode}"
            body = args.get("body") or args.get("extra") or args.get("content") or ""
            if body:
                content += f" {body}"
        elif action == "set_mode":
            content = f"set_mode {value or name}"
        elif action == "switch_model":
            content = f"switch_model {value or name}"
        elif action == "set_theme":
            content = f"set_theme {value or name}"
        elif action == "create_theme":
            colors = args.get("colors", {})
            theme_name = name or value or "custom"
            bg = colors.get("bg", "#282c34")
            fg = colors.get("fg", "#9cdef2")
            panel = colors.get("panel", "#111111")
            border = colors.get("border", "#355a66")
            accent = colors.get("accent", "#e06c75")
            content = f"create_theme {theme_name} {bg} {fg} {panel} {border} {accent}"
            # Append advanced overrides as key=value
            adv_keys = [
                "userBubbleBg", "aiBubbleBg", "bubbleBorder", "sidebarBg",
                "sectionAccent", "brandColor", "inputBg", "inputBorder",
                "sendBtnBg", "sendBtnHover", "codeBg", "codeFg",
                "toggleBg", "toggleActive", "accentPrimary", "accentError",
            ]
            for ak in adv_keys:
                if colors.get(ak):
                    content += f" {ak}={colors[ak]}"
        else:
            content = action
    elif tool_type in ("manage_tasks", "manage_skills", "api_call",
                        "manage_endpoints", "manage_mcp", "manage_webhooks",
                        "manage_tokens", "manage_documents", "manage_settings"):
        content = json.dumps(args)
    elif tool_type == "ask_teacher":
        content = args.get("model", "auto") + "\n" + args.get("problem", "")
    elif tool_type == "ask_user":
        # Keep user-facing labels readable in the tool trace.  The outer SSE
        # JSON encoder will escape them for transport and JSON.parse restores
        # them once; pre-escaping here caused literal ``\u00f1`` sequences to
        # remain visible in the debug panel.
        content = json.dumps(args, ensure_ascii=False)
    else:
        content = json.dumps(args)

    return ToolBlock(tool_type, content)
