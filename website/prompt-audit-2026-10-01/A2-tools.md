# A2 audit: tool schemas and tool-facing text

Scope: `src/tool_schemas.py`, `mcp_servers/*.py`, `src/tool_index.py`, result and refusal strings in `src/tool_execution.py`, `src/agent_tools/*`, `src/agent_worktree/*`, `src/worker_preflight.py`, `src/worktree_writers.py`.
Standard: writing-for-agents (pointer wording, leading words, positive targets, single source of truth, no-ops, sprawl) and unslop.
Read-only audit. Nothing in the repository was edited.

## How the wire payload is built (read this first, it changes the weights)

`compact_function_tool_schemas` (src/tool_schemas.py:110, called from src/agent_loop.py:5846) rewrites every native schema before it is sent. It keeps whole leading sentences up to 400 chars per tool description and up to 160 chars per parameter description. Only `manage_git` and `manage_agent_worktree` are exempt (`_UNTRIMMED_TOOLS`, line 50). So the model sees 91.5 K chars of schema text for all 87 native tools, not the 109.9 K in the source. Text past the cut is never sent. Weights below use the compacted size. The "full" column is the source size.

Log facts (`app.log`, 542 rounds with `[agent-debug] tools_sent=`): average 35.6 tools per round, range 16 to 102. The `tool_names=[...]` field is capped at 15 names and `relevant_tools=[...]` at 15, so "times sent" is the larger of the two counts for each tool. Tools beyond the cap (the loadout, delegate and skills tools) are therefore a lower bound. 1117 `Tool executed` lines.

## 1. Weight table (top 15 by compact chars x times sent)

| # | Tool | Full chars | Wire chars | Times sent | Times called | Weight (chars x sent) |
|---|------|-----------:|-----------:|-----------:|-------------:|----------------------:|
| 1 | manage_agent_loadout | 9145 | 6273 | 327 | 49 (17 start, 26 status, 2 stop, 2 get, 2 list, 0 create/update/export/import) | 2,051,271 |
| 2 | manage_git | 4011 | 4011 | 369 | 61 (27 status, 15 log, 10 diff, 3 fetch, 2 stage) | 1,480,059 |
| 3 | manage_skills | 3721 | 2880 | 473 | 10 | 1,362,240 |
| 4 | manage_agent_worktree | 3450 | 3450 | 369 | 39 (11 status, 7 diff, 6 commit, 6 request_publish, 5 show_request) | 1,273,050 |
| 5 | delegate_to_claude_code | 4025 | 2231 | 344 | 4 | 767,464 |
| 6 | ask_user | 1412 | 1199 | 542 | 0 | 649,858 |
| 7 | grep | 1024 | 1024 | 542 | 185 (17 timed out) | 555,008 |
| 8 | preview_file | 1487 | 1020 | 430 | 60 | 438,600 |
| 9 | edit_file | 939 | 939 | 438 | 66 (12 refused or failed) | 411,282 |
| 10 | ls | 726 | 726 | 542 | 25 | 393,492 |
| 11 | web_fetch | 847 | 692 | 542 | 3 (3 failed) | 375,064 |
| 12 | bash | 877 | 760 | 450 | 65 | 342,000 |
| 13 | glob | 623 | 623 | 542 | 39 | 337,666 |
| 14 | manage_memory | 707 | 707 | 473 | 0 | 334,411 |
| 15 | web_search | 529 | 529 | 542 | 4 | 286,718 |

Next in line: discover_tools (505 x 542, 0 calls), read_file (490 x 542, 285 calls), apply_patch (549 x 438, 22 calls, 8 failed), get_workspace (440 x 542, 20 calls), write_file (322 x 438), recall_chat_history and recall_tool_output (1208 and 1100 x 104).

Reading the table: six of the top 15 are called zero to ten times per hundred sends (loadout create/update/export/import params, manage_skills, delegate_to_claude_code, ask_user, manage_memory, web_*). They are paid on every round. The most-called tools (read_file 285, grep 185) are small, so their cost is correctness (A2-3, A2-4), not size.

## 2. Findings

### A2-1. Clipping to 400/160 chars drops the routing text of the guidance-heavy tools
- Where: src/tool_schemas.py:35-50 and :110-138 (cap and clip); affected texts at :286 (preview_file), :948 (ask_user), :1153 (manage_skills), :1502 (manage_agent_loadout), :1614 (delegate_to_claude_code), recall_chat_history, recall_tool_output.
- Load: every round for every tool; 91.5 K wire chars of 109.9 K source.
- Lever: correctness (the clip keeps leading sentences, assuming they carry the use; here they carry identity and the rules sit after the cut). Also leading word placement.
- Evidence: the model receives for `preview_file` only "Render an HTML, SVG, PNG, JPEG, WebP or GIF file from the workspace (or its worktree) and return a screenshot you can SEE." The sentence telling it to compare against the reference before saying visual work is done is cut. For `ask_user` the cut removes "Prefer sensible defaults over asking". For `manage_skills` the cut removes "do not 'list' to see what exists" and the `names=[...]` batching rule. For `delegate_to_claude_code` it removes "poll with wait_seconds instead of sleeping in bash" and "never install or update Claude Code from bash". The `action` parameter of `manage_agent_loadout` arrives as only "Default list. export = the saved loadouts (all, or 'names') as a portable JSON document." (the least used branch is the one the model reads).
- Fix: author each description so its first 400 chars are the complete pointer (what it is, the branches that make the model reach for it, the one rule that prevents the common mistake). Put the 160-char parameter budget on the branch that matters first. Add a test that fails when a native description exceeds 400 chars or a parameter exceeds 160, so clipping becomes a no-op and authors see what ships. The discovery path (src/tool_discovery.py:160) uses a different cut (first ". " sentence, 220 chars): align it to the same budget.
- Impact: high. Every other finding about long descriptions depends on this; today the model acts on text nobody reviewed.

### A2-2. manage_agent_loadout ships 6.3 K chars per round; 43 of 49 calls use start/status/stop
- Where: src/tool_schemas.py:1502-1612.
- Load: sent 327+ times; 6273 wire chars (full 9145). Never called with create, update, delete, export, import in this log.
- Lever: sprawl; rules only one branch needs; split by invocation (writing-for-agents "When to split").
- Evidence: 42 properties. About 26 exist only for create/update (`temperature`, `max_tokens`, `model_fallbacks`, `allowed_models`, `memory_access`, `skill_access`, `skill_names`, `mcp_access`, `allowed_mcp_servers`, `delegation_policy`, `persona_name`, `instructions`, `shell_access`, `approval_mode`, `required_tools`, `clear`, ...). Import/export add `document`, `mode`, `rename_conflicts`, `names`.
- Fix: split into two tools. `manage_agent_loadout` keeps `action` (start, status, stop, list, get, capabilities, preflight), `name`, `task`, `workspace`, `requires`, `extra_tools`, `parallel`, `run_id`, `wait_seconds`, `worker_session`, `detail`. A second tool `edit_agent_loadout` (create, update, delete, export, import and all policy fields) is attached by `discover_tools` or by keyword. Proposed pointer for the first tool (under 400 chars):
  "Workers: start one detached worker chat per whole task, watch it with status (wait_seconds blocks until it finishes), stop it, or list saved loadouts. A finished worker's result is handed back to this chat and kept in result_ref, readable with recall_tool_output. The worker has not seen this conversation, so `task` carries the full brief."
  `action` (under 160): "start (needs task) | status | stop | list | get | capabilities | preflight. Default list."
  Move "a loadout you create is intersected with this chat's policy" into the second tool.
- Impact: high. About 3.5 K chars saved per round on the largest tool and the start/status text finally reaches the model.

### A2-3. `grep: timed out` gives no cause and no next step and throws away the hits
- Where: src/agent_tools/filesystem_tools.py:1123, 1213, 1218, 1333, 1385-1386 (error return); deadline `_GREP_TIMEOUT_SECONDS = 20` at :26.
- Load: 17 of 185 grep calls (9%) in this log, including a search rooted at `static` with `glob: *.html`.
- Lever: correctness (error without a next step), plus a likely concurrency bug.
- Evidence: `Tool executed: grep: {"pattern": "agamemnon-mockup.css", "path": "static", "glob": "*.html" ... error=grep: timed out`. The model sees the three words and repeats a broader or similar search. Lines already matched are dropped (`return None, error`).
- Fix: return the matches found so far with a note, and make the message actionable: "grep: stopped after 20 s with N matches so far (shown above). Narrow `path` or `glob`, or search a literal string." Check why a small directory times out: five parallel native calls ran in one round (log line "5 native calls"), so the 20 s deadline may include queue wait for the shared worker. Start the deadline when the search starts.
- Impact: high. Most-called search tool, 9% failure, silent loss of partial results.

### A2-4. read_file truncates at 20,000 chars and neither the description nor the notice says how to continue
- Where: src/tool_schemas.py:222 (description); src/agent_tools/filesystem_tools.py:381-382 (notice); src/constants.py:80 (`MAX_READ_CHARS`).
- Load: 285 calls, the most-called tool; 490 chars sent 542 times.
- Lever: correctness (description omits a behaviour limit); missing next step.
- Evidence: description "Read a file from disk. Optionally read a line range with offset/limit for large files." Result tail: `... [truncated at 20000 chars]`.
- Fix: description: "Read a file (first 20,000 characters, or lines offset..offset+limit). Page with offset and limit when the result says truncated." Notice: `... [truncated at 20000 chars; call again with offset=<next line> to continue]`, ideally naming the line number where the cut fell. Also `not found` at :374 gets "(use glob or ls to find it)".
- Impact: high. It is the single most-used tool and the continuation path is guesswork (the log shows many `offset: 1, limit: 500` calls, i.e. the model learned the limit by trial).

### A2-5. manage_git: 4 K chars, never trimmed, restates itself and the parameter list
- Where: src/tool_schemas.py:387-436; exempt from compaction at :50.
- Load: sent 369 times, 4011 chars, 61 calls (status, log, diff dominate).
- Lever: duplication (the first two sentences say the same thing; the action list is both in the description and the `action` enum; "only" scoping repeated in 14 parameter descriptions), prohibitions that should be positive, missed context pointer (what branch makes the model reach for the tool).
- Evidence: "Scoped Git workflows (including pull/push): send only fields used by the chosen action; omit unused fields. Git workflows in approved local checkouts (configured roots and the active workspace's checkout): repositories, status, diff, log, branches, remotes, clone, init, stage, ..." followed by the full enum list again.
- Fix (about 750 chars, replaces about 1900):
  "Git in approved local checkouts, no shell needed. Start with action=repositories for the absolute paths. Read actions: status, diff, log, branches, remotes. Change actions: stage (explicit relative paths), commit, branch, tag, switch, fetch, pull, stash_*, push, merge, reset, rebase, set_upstream, clone, init. Send only the fields the action uses. Pull and merge are fast-forward only. Push, merge, reset and rebase need expected_head and a human confirmation. Delete, force-push and discard are outside policy. A linked worktree allows only the read actions; commit there with manage_agent_worktree."
  Add the real diff behaviour, which the text omits: `diff` covers the whole tree and is capped at 256 KB with a `truncated` flag (src/agent_worktree/repository_local.py:43, 524); say "for one file use bash `git diff -- <path>`". Drop the unused `expected_target` zero-SHA prose from the description and keep it on the parameter.
- Impact: high. About 1.3 K chars per round saved, and the diff scope gap is a repeat surprise (10 diff calls).

### A2-6. manage_agent_worktree: 3.4 K chars, front-loads the legacy repo_* actions, stale scope
- Where: src/tool_schemas.py:437-500.
- Load: sent 369 times, 3450 chars; calls were status, diff, commit, request_publish, show_request; repo_list once, repo_status/repo_pull never.
- Lever: stale text; duplication with manage_git (`repo_list`/`repo_status`/`repo_pull` repeat `repositories`/`status`/`pull`); leading words; prohibitions.
- Evidence: opens "Choose an action and omit unused fields; repo_list needs only action, repo_status/repo_pull also need repository." The src/tool_index.py entry for the same tool calls those actions "Legacy". The description also says "WITHOUT 'repository' they are made of the Odysseus source checkout", while the live case is any repository (src/agent_worktree/service.py `repository_config`).
- Fix (about 1.5 K): lead with the worktree lifecycle and drop repo_* from the prose (keep them in the enum with "use manage_git for repository listing and sync"):
  "Isolated, human-gated publishing worktree for a repository. start (name = task name, base = origin/main, a branch or a SHA) makes branch agent/<repo>/<name>; status, diff and commit work in it; request_publish freezes the change and asks a person to approve (pushes nothing); show_request and list_requests follow it; cleanup removes a clean worktree. Pass `repository` (absolute path from manage_git repositories) for any project, and the same value on later calls. publish needs a request_id and an approval_code a person gives you; you cannot approve your own change."
  Parameter descriptions shorten to the field's own facts ("start only: ...").
- Impact: medium to high. 1.5 K chars per round, plus removes a second route for repository listing.

### A2-7. manage_skills: 21 parameters, 13 only for `add`, repeated "(for add)"
- Where: src/tool_schemas.py:1153-1260.
- Load: sent 473 times, 2880 wire chars, 10 calls.
- Lever: sprawl; duplication (`name`, `description`, `category` each say how they are used under add, the action enum description restates every action, the tool description restates the lifecycle); wrongly ordered clip (A2-1).
- Evidence: `"description": "Numbered steps (for add)."`, `"Known failure modes + recovery (for add)."`, `"Keyword tags (for add)."` and so on for 13 fields.
- Fix: put the behaviour rule first and shorten. Description: "Skills: view full SKILL.md procedures (names=[...] loads several in one call; the skills index is already in your context, so list is rarely needed), or save a new or changed skill: add, edit (full content), patch (old_string to new_string), publish once the procedure has worked. Report the name the tool returns." `action` (under 160): "view | view_ref | search | add | edit | patch | publish | list | delete. add needs a kebab-case name." Move the add-only fields (`when_to_use`, `procedure`, `pitfalls`, `verification`, `tags`, `platforms`, `requires_toolsets`, `fallback_for_toolsets`, `category`, `version`, `confidence`) behind one `fields` object parameter, or into a second `author_skill` tool attached on demand.
- Impact: medium. About 1.4 K chars per round and the lead now says when to reach for it.

### A2-8. delegate_to_claude_code: 4 K chars of branch detail, 4 calls in 344 sends
- Where: src/tool_schemas.py:1614-1700; near-duplicate `delegate_to_agent` at the next schema.
- Load: sent 344 times, 2231 wire chars (4025 source), 4 calls.
- Lever: sprawl (cloud-runner, update, model-alias list, allowed_tools grammar only matter to one branch each); negation ("NOT a chat model — do not use chat_with_model/list_models", "never install or update Claude Code from bash"); incident prose in `model` (a list of model IDs).
- Evidence: `model`: "Claude Code runs Claude models only: an alias (opus, sonnet, haiku, fable, opusplan) or a Claude model ID such as claude-opus-5-5, ... Spellings like 'opus-5.5' are normalised; gpt-*, o3, gemini and other providers' models are rejected."
- Fix: description (under 400): "Claude Code CLI as a coding agent for a bounded task in an approved repository: it inspects, edits, tests and commits, never pushes. action=status first when unsure it is installed; start returns a task_id, poll with wait_seconds (use it for audits and multi-file work); run waits for short jobs. A GitHub owner/repo goes to the cloud runner and returns a draft PR." `model`: "A Claude alias (opus, sonnet, haiku) or Claude model ID; omit for the default." Move `allowed_tools` grammar, `update` and `base_branch` text into the error/status result where they are needed. Consider removing `delegate_to_agent` from the sent set whenever this tool is present.
- Impact: medium. About 700 wire chars per round; sharper pointer.

### A2-9. ask_user: negations and a duplicated options description
- Where: src/tool_schemas.py:948-980.
- Load: sent 542 times (every round), 1199 wire chars, 0 calls in this log.
- Lever: prohibition as positive target; no-op; duplication (`options` repeats the inner `label` and `description` text).
- Evidence: "Do NOT use it to confirm irreversible/destructive actions that have a dedicated confirmation flow." and "Set true ONLY when ... Otherwise omit it or set false. Default false."
- Fix: "Ask: a multiple-choice question when the answer changes what you do next and a default would be a guess (approach, assumption, target). Calling it ends your turn; the user's pick arrives as the next message. Destructive actions have their own confirmation flow." `question`: "Specific and self-contained." `options`: "2-6 choices, each a short `label` with an optional one-line `description`." `multi`: "True when several options may be picked." Delete the inner label/description prose.
- Impact: medium. About 550 chars per round on a tool sent on every round.

### A2-10. File tool descriptions repeat the same advice three times and omit real limits
- Where: src/tool_schemas.py:238 (grep), :256 (glob), :271 (ls), :327 (edit_file), :344 (apply_patch), :312 (write_file); the same rules again in src/agent_loop.py:444-460, 796, 808.
- Load: grep 542 sends, ls 542, glob 542, edit_file 438; 2.3 K chars together.
- Lever: duplication with the system prompt's File rules; no-ops; single source of truth; stale/omitted behaviour.
- Evidence: "PREFER this over `bash grep/rg`" (grep), "PREFER this over `bash find/ls`" (glob), "PREFER this over `bash ls`/`find`" (ls), "PREFER this over bash (sed/echo)" (edit_file), "Prefer this over bash redirects/heredocs/sed" (apply_patch), and the system prompt says "Prefer `grep`, `glob`, and `ls` over shell equivalents". "run `ls` with depth first" is repeated in grep, glob and ls. The `ls` result hides dotfiles (filesystem_tools.py:752 `entry.name.startswith(".")`) and caps at 200 entries; neither is stated. grep caps at 200 hits and 400 chars per line; not stated.
- Fix, one line per tool (bash and the system prompt own the "not bash" rule):
  - grep: "Search file contents by regex (ripgrep, .gitignore respected). Returns file:line:match, at most 200 hits. Pass the narrowest `path` and a `glob` for a large tree."
  - glob: "Find files by glob, newest first, at most 200. After `ls` depth 2 shows the layout, glob inside the folder that matters."
  - ls: "List a directory (dotfiles hidden). depth 2-4 returns a folder outline with file counts, skipping build, vendor and cache folders; use it as the first look at an unfamiliar tree."
  - edit_file: "Replace exact text in a file on disk. `old_string` must match once, indentation included, or set replace_all. write_file creates a new file; edit_document edits editor-panel documents." (drops the `~/sweden.txt` example and the diff claim.)
  - apply_patch: "Apply related edits across files in one patch (*** Begin Patch ... *** End Patch with Add File, Update File, Delete File sections)." Drop the parameter text that repeats the first sentence.
- Impact: medium. About 1.2 K chars per round across the five tools, and the dotfile gap prevents `ls` misses.

### A2-11. Edit and patch failures say "match exactly" without a hint
- Where: src/agent_tools/filesystem_tools.py:278, 281, 617, 622, 625.
- Load: edit_file 12 of 66 calls failed (7 on the worker-writer refusal, 2 on no match); apply_patch 8 of 22 failed (hunk context not found, matched 2 and 3 times, one worker refusal).
- Lever: correctness (error does not say what to do instead).
- Evidence: `apply_patch: .../style.css: hunk 1 context matched 2 times`; `hunk 1 context not found`; `old_string not found in ... Read the file and match it exactly.`
- Fix: `hunk N context matched K times: add unchanged lines around the change until it is unique, or use edit_file with replace_all`; `hunk N context not found: re-read the file and copy the context lines exactly, including whitespace`. For edit_file not found, include the closest 3 lines of the file by similarity, which removes the re-read round.
- Impact: medium. Cuts a retry round on a 36% failing tool.

### A2-12. Refusals in tool_execution.py end without a next step
- Where: src/tool_execution.py:1798, 1837, 1869, 1920-1925, 2003-2004, 2011, 2438.
- Load: not frequent in this log (the worker-writer refusal and git linked-worktree refusal dominate and are good), but these are the strings the model gets when a tool is withheld.
- Lever: correctness (error without a next step; refusal that leaves the model nothing to do).
- Evidence: "Tool 'X' is disabled by user." / "is not in this agent's tool allowlist (its selected tool bindings)." / "forbade by the active guide-only policy." / "requires an admin user." / "Unknown tool: X".
- Fix: add the move to each. Allowlist: "report that you need X to the chat that started you, or finish the step with a tool you have." Disabled: "continue without it, and tell the user it is turned off." Admin: "state what you needed so an admin can do it." Unknown: "Unknown tool: X. Call discover_tools with what you need, or use a name from your tool list." Also fix the grammar "forbade" (should be "forbidden").
- Impact: medium. Removes dead ends and retries when a worker's tool set is narrow.

### A2-13. Path refusals do not name what is allowed
- Where: src/tool_execution.py:707, 830-833 ("is outside the allowed roots"), :804, :859-865 ("sensitive directory"), :976 ("outside the workspace").
- Load: not seen as a recurring failure in this log, but every file tool uses it.
- Lever: correctness (refusal sends the model in circles).
- Evidence: `path 'X' is outside the allowed roots` (adds only a personal-docs suggestion).
- Fix: "path 'X' is outside the allowed roots. The workspace is <ws>; call get_workspace for the folders you may use, or pass a path inside one." For sensitive: "... is a protected file (keys, credentials); ask the user to paste the part you need."
- Impact: low to medium.

### A2-14. Workspace parameter text contradicts the refusal that follows (4 repeated failures)
- Where: src/tool_schemas.py (`manage_agent_loadout.workspace`, `send_to_session.workspace`); src/worker_preflight.py:550.
- Load: 4 identical failures of `send_to_session` in this log ("Worker not started: the task works on a repository but no workspace is set; pass workspace as one of: ...").
- Lever: correctness (parameter description disagrees with behaviour); the model followed "Omit to use this chat's workspace".
- Evidence: "Omit to use this chat's workspace, or the checkout the task names." while the chat has no workspace.
- Fix: `workspace`: "The checkout the worker's file tools work in (a path from get_workspace). Required when this chat has no workspace and the task touches a repository." Keep the preflight message (it names candidates and `retry_with`), but start it with the action: "Pass workspace=<one of ...>; this chat has none."
- Impact: medium. The same call failed four times, so the text is not steering.

### A2-15. web_fetch refuses SVG and gives a dead-end next step
- Where: src/agent_tools/web_tools.py:187; src/tool_schemas.py:207.
- Load: 3 of 3 web_fetch calls failed, all `https://api.iconify.design/game-icons/*.svg` (about 700 chars sent 542 times).
- Lever: correctness (the tool rejects a text-bearing content type it should return); error text points to "the browser tool if one is enabled", which is blocked on `file:` and the dev server in this log.
- Evidence: `web_fetch: https://api.iconify.design/game-icons/crested-helmet.svg: no readable text content (not HTML, or the page needs JS/login). Open it with the browser tool if one is enabled, or use web_search`.
- Fix: return the body for `image/svg+xml`, `application/json`, `text/*` and other text types. For a true failure say "binary or empty response (content-type X)". For icons, `search_icons` with `ids` returns full SVG with the licence; one line in the description of web_fetch is not needed once the tool returns SVG text.
  Description (replaces the negations and the examples): "Fetch a known URL as readable text (HTML, JSON, SVG, plain text). Large bodies come back with a [partial content] notice; call again with full=true for the rest. For finding pages, use web_search." `full`: "Raise the download budget to the hard cap."
- Impact: medium. A permanent failure on a sent-every-round tool, plus 150 chars saved.

### A2-16. web_search points at trigger_research, which is not in the sent list
- Where: src/tool_schemas.py:192, :207.
- Load: web_search 542 sends, web_fetch 542; trigger_research was never sent in this log (0 mentions).
- Lever: correctness (description names a tool the model may not have); negation.
- Evidence: "NOT for 'research X' / 'do research on X' — those are deep-research jobs; use trigger_research instead."
- Fix: web_search: "Quick lookup of one fact or current event. Longer 'research X' jobs go to trigger_research (discover_tools if it is not attached)." Drop the same sentence from web_fetch (it is a URL tool; the redirect belongs to web_search only).
- Impact: low to medium. Also saves about 200 chars per round.

### A2-17. preview_file carries an incident note, and the clipped version loses the rule it exists for
- Where: src/tool_schemas.py:286-310.
- Load: 430 sends, 1020 wire chars (1487 source), 60 calls.
- Lever: sprawl (incident note); leading word; stale-prone parenthetical.
- Evidence: "(an 'Agamemnon helmet' SVG once passed every string check and drew headphones)". After clipping the model sees only "Render ... and return a screenshot you can SEE."
- Fix: "See your output: renders an HTML, SVG or raster file from the workspace or its worktree to a screenshot. Open it before reporting visual work done and compare it with the reference image or the request, since passing tests do not show the shape is right. Read-only; network is blocked, so backend and CDN content renders partially. Path rules as read_file." Delete the incident story (it lives in src/agent_tools/preview_tools.py already). Parameters: drop the duplicated "default" prose (`scale` and `color_scheme` repeat the default twice).
- Impact: medium. Same size, the rule survives the clip.

### A2-18. manage_memory description is a no-op and gives no branch
- Where: src/tool_schemas.py:869 and mcp_servers/memory_server.py:120.
- Load: sent 473 times, 707 chars, 0 calls; two copies (native and MCP) of the same text.
- Lever: no-op (restates the tool name and the enum); missing branches; duplication between two sources.
- Evidence: "Manage the user's memory system: list, add, edit, delete, or search memories. Memories persist across sessions."
- Fix: "Memory: durable facts, events, contacts and preferences that outlive this chat. add when the user states something to remember or a lasting preference; search before asking the user for something they may already have told you; edit or delete by memory_id taken from list or search." One description constant imported by both places. Parameters keep their one-line texts.
- Impact: low to medium. Same size, the model learns when to call it (0 calls in 473 sends suggests it is not).

### A2-19. discover_tools is a no-op pointer
- Where: src/tool_schemas.py:147-159.
- Load: 542 sends, 505 chars, 0 calls.
- Lever: no-op ("Discovery is read-only and does not execute a discovered tool"); parameters have no description.
- Fix: "Attach tools that are not in your list: describe what you need ('read a Todoist task', 'render a Penpot board'). Matches are callable on your next round, at most 8." `query`: "The capability you need, in plain words." `max_results`: delete (the default is enough).
- Impact: low. Shorter and it names the branch.

### A2-20. bash: duplicated timeout text between description and parameter
- Where: src/tool_schemas.py:163-174.
- Load: 450 sends, 760 wire chars, 65 calls.
- Lever: duplication; a prohibition ("not redirects, heredocs or sed") that repeats the system prompt (src/agent_loop.py:744).
- Evidence: description "A command silent for 60 s is stopped: drop -q/--silent from builds and tests, raise `idle_timeout`, or make `#!bg` its first line" and the parameter "Seconds this command may print nothing before it is stopped (default 60, max 3600). Raise it only for a command known to be quiet for long stretches, e.g. a test suite that prints once per test class."
- Fix: description: "Run a shell command (on the host or in a workspace sandbox, per this chat's shell note): installs, builds, tests, git, programs. Start long commands with `#!bg` as the first line to run them in the background." `idle_timeout`: "Seconds of silence before the command is stopped (default 60, max 3600). Raise it for quiet builds."
- Impact: low. About 160 chars per round.

### A2-21. search_icons and render_preview (Penpot studio MCP): required `query` that is "ignored", typo, implementation prose
- Where: mcp_servers/penpot_studio_server.py:53-77 (search_icons), :124-137 (render_preview), :70 (negation).
- Load: used in the designer loadout (28 inspect_design, 31 render_preview calls in this log).
- Lever: correctness (parameter validation disagrees with the sensible use); negation; sprawl; typo.
- Evidence: `ids`: "(query is still required but ignored then; repeat what you searched)". The server rejects a call without `query` (the generic required check at :186). render_preview: "(never an error image).Mints a short-lived view-only link that is deleted afterwards." The search_icons description: "DO NOT hand-draw SVG paths: search here".
- Fix: make `query` required only when `ids` is absent (change `required` to `[]` and validate in `call_tool`). search_icons: "Search 200k open vector icons (game-icons has helmets, swords, soldiers; tabler, lucide, phosphor, material) and return ids like 'game-icons:spartan-helmet' with each set's licence. For a logo, emblem or illustration in code, a page or a file, search here, then call with ids=[...] (max 6) for each icon's standalone <svg> markup, licence and the attribution line to paste into ACKNOWLEDGMENTS. CC BY sets need that credit." render_preview: "See a board: renders a top-level frame with Penpot's own renderer and real fonts. Use it after building and after each fix, and look for overlap, clipping, low contrast and broken icons. An error means Penpot's viewer showed its error page." Delete the "mints a view-only link" sentence (implementation, not a decision input).
- Impact: low to medium. Removes a wasted parameter and a typo in a model-facing string.

### A2-22. tool_index.py entries are stale for the tools that changed most recently
- Where: src/tool_index.py (the dict that holds `manage_agent_worktree`, `manage_git`, `manage_skills`, `manage_agent_loadout`).
- Load: embedding documents for retrieval; they decide which tools are selected, so a stale entry means a stale pick.
- Lever: stale text.
- Evidence: `manage_agent_worktree`: "Odysseus's persistent agent/odysseus/* worktree ... start/status/diff/commit/request_publish/publish/list_requests/show_request/remove ... Legacy repo_list/repo_status/repo_pull actions" while the schema action is `cleanup`, not `remove`, and worktrees now exist for any repository. `manage_skills`: "Skill management: add, update, publish, or search reusable skills/presets." (no `view`, the call the model makes most often). `get_workspace` in the index differs from the schema text.
- Fix: generate the index document from the schema's first sentence plus a hand-written "use for" list, or drop the stored copy for tools that have a schema. For these four, update: `cleanup` for `remove`, "any repository" for the scope, add `view` to skills.
- Impact: low to medium. Retrieval quality for the very tools in this audit's top 5.

### A2-23. Smaller items
- `get_workspace` description ends "Takes no arguments." (no-op; the schema has no properties). src/tool_schemas.py:305. 30 chars.
- `manage_git` and `manage_agent_worktree` error `local repository operation failed safely` (src/agent_worktree/repository_local.py:740) hides the cause from the model (5 manage_git failures, 1 of them this text). Fix: include the exception class and the first line, redacted: "local repository operation failed (<ExcType>: <first line>); run status and retry once, or report it".
- recall_tool_output: the invalid-ref error (src/agent_tools/rag_tools.py:230) names the format but not the ids that exist. The model sent `toolout-c5bb1193-1d32-5b35-...` and `toolout-487f3a9a...` (made-up long forms). Fix: append "Stored for this chat: toolout-ce6d25e607, ..." (up to 5). Its description (1566 chars) also spells out paging rules that the result already prints ("next offset"); shorten to the three modes (ref, ref+offset, query).
- email: `mcp_servers/email_server.py` and the native `BUILTIN_EMAIL_TOOLS` schemas describe the same tools with different text (audit_emails 1224 vs 2880 chars; reply_to_email and send_email differ). Not sent on dev chats, but two sources of truth for the same behaviour. Pick one constant.
- `ui_control` (4292 chars) lists 16 theme names inline and a CAPS rule ("When a user asks for ANY theme not in the built-in preset list, ALWAYS use create_theme"). Move the theme list into the `set_theme` parameter enum, state the rule once. Admin-keyword tool, not in the common set.
- Em dashes appear throughout tool descriptions (unslop rule 13). Replace when a description is rewritten anyway.

## 3. Top 5 by impact
1. A2-1 Clipping drops the routing text of the guidance-heavy tools (systemic; every description is judged on the first 400 chars).
2. A2-2 manage_agent_loadout: split the create/update/import/export half out; 3.5 K chars per round.
3. A2-3 `grep: timed out` with no next step and lost partial hits (9% of the most-called search tool).
4. A2-5 / A2-6 manage_git and manage_agent_worktree rewrite (untrimmed, duplicated, stale, 2.8 K chars per round).
5. A2-4 read_file truncation unannounced and without a continuation hint (285 calls).

## 4. Estimated chars saved across the top-15 tools (per round when all are sent)
| Tool | Wire chars now | After | Saved |
|------|---------------:|------:|------:|
| manage_agent_loadout | 6273 | 2300 | 3970 |
| manage_git | 4011 | 2300 | 1710 |
| manage_agent_worktree | 3450 | 2000 | 1450 |
| manage_skills | 2880 | 1500 | 1380 |
| delegate_to_claude_code | 2231 | 1500 | 730 |
| ask_user | 1199 | 650 | 550 |
| edit_file | 939 | 520 | 420 |
| web_fetch | 692 | 450 | 240 |
| ls | 726 | 520 | 210 |
| grep | 1024 | 800 | 220 |
| web_search | 529 | 330 | 200 |
| bash | 760 | 600 | 160 |
| glob | 623 | 450 | 170 |
| preview_file | 1020 | 900 | 120 |
| manage_memory | 707 | 650 | 50 |
| Total | 32,064 | 15,470 | about 11,600 |

About 11.6 K chars (roughly 3 K tokens) per round, about 36% of the top-15 tools' schema text. Weighted by sends in this log (542 rounds), that is about 5.2 M chars less schema traffic. The larger gain is that the first 400 chars of each description now carry the rules the model currently never receives.
