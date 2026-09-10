# Tool-schema hygiene — 2026-09-10

## Evidence

- `[agent-debug] … schema_without_selection=[create_session, list_models,
  list_sessions, manage_endpoints, manage_mcp, manage_session,
  manage_settings, manage_tokens, manage_webhooks, pipeline,
  send_to_session]` on all 28 rounds of a coding turn: `_needs_admin` matched
  a keyword ("task", "doc", "chat", …) once, and `_tool_schemas_for_round`
  unioned the entire `_ADMIN_TOOLS` set into every round — about 1.3k schema
  tokens per round that nothing selected.
- `[agent] missing-tool self-unblock … re-armed 90 tool(s)`: one "no GitHub
  publishing tool" claim widened to the whole registry; the next round sent
  118 schemas (14,180 schema tokens) with zero cache.
- `[tool-rag] Low-signal query; will run RAG retrieval` for "i like Umni"
  returned `browser_drag`, `scan_email_unsubscribes`, `read_app_logs`,
  `write_file` — the index has no similarity cutoff, so every turn gets its
  eight nearest tools whatever the distance.
- The agent probed `claude --help` for three rounds and then ran `claude -p`
  from `bash`, bypassing the delegation tool's allowlist, restricted mode,
  per-repository lock and task tracking.
- Every turn with retrieval logged `latest user context mismatch` and
  appended the user's request a second time.

## Priorities

1. Admin tools by matched keyword (`_ADMIN_KEYWORD_TOOLS`,
   `_detect_admin_tools`): "add a task" adds `manage_tasks`, "delete this
   chat" adds the session tools, only "admin" adds the whole set.
2. Targeted self-unblock (`_targeted_rearm_tools`): re-arm the tools the
   claim names, the keyword intents it triggers (a push claim surfaces
   `manage_agent_worktree`) and the index's nearest neighbours for that text;
   the full-registry widening is the fallback and is what the re-arm cap
   counts.
3. Low-signal turns select with keyword/structural hints only
   (`get_tools_for_query(use_embeddings=False)`); domain seeding, retained
   tools and the self-unblock still cover a miss.
4. The shell tool redirects a direct `claude` invocation to
   `delegate_to_claude_code` (`src/agent_tools/claude_code_guard.py`), the
   same way it redirects `git push`. Escape hatch:
   `ODYSSEUS_AGENT_ALLOW_BASH_CLAUDE=1`.
5. `_last_user_plain_text` skips appended untrusted-context blocks, so the
   grounding helper no longer duplicates the request.

## Expected effect

Per round on an admin-flavoured coding turn: ~1.3k fewer schema tokens
(≈25–30% of the schema block). Per low-signal chat turn: ~1.2k fewer schema
tokens and a stable, minimal tools prefix. A self-unblock costs a handful of
schemas instead of ~14k tokens per remaining round. One fewer copy of the
user's request per retrieval turn.

## Acceptance criteria

- `tests/test_harness_efficiency_specs.py`: keyword→tool mapping, schema
  builder honours the matched set, low-signal selection never calls the
  embedding index.
- `tests/test_agent_targeted_rearm.py`: named tools, push claims and index
  neighbours are re-armed; nothing specific means an empty set.
- `tests/test_claude_code_bash_guard.py`: `claude …` at a command position is
  redirected; `grep claude`, `python claude_tool.py` still run.
- `tests/test_chat_latest_user_grounding.py`: an appended untrusted block is
  not the latest user message.
