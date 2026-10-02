# Prompt audit, 2026-10-01

Every piece of text a model reads in this repository, checked against the
writing-for-agents rules (pointer wording, completion criteria, positive
targets, no-ops, single source of truth, disclosure, leading words) and for
correctness. Four read-only passes produced 100 findings, 23 of them rated high
or medium-to-high.
Each has a location, a quote and a concrete fix in the detail files:

| Area | Detail | Findings (high) |
|---|---|---|
| Agent system prompt and loop messages (`src/agent_loop.py`, `src/prompt_security.py`) | [A1](prompt-audit-2026-10-01/A1-system-prompt.md) | 29 (8) |
| Tool schemas, MCP tool descriptions, tool results and refusals | [A2](prompt-audit-2026-10-01/A2-tools.md) | 23 (6) |
| Worker orchestration messages, loadout responses, agent-facing docs | [A3](prompt-audit-2026-10-01/A3-harness-messages.md) | 24 (3) |
| One-shot and background prompts (email, research, compaction, memory, skills, scheduler) | [A4](prompt-audit-2026-10-01/A4-llm-prompts.md) | 24 (6) |

The finetuned `_minimal_odysseus_*` prompts were left alone: the local model is
trained on those exact strings.

## 1. Untrusted content and instructions share one channel

This is the most serious group. Some of these paths run unattended and change
things.

- **Email bodies reach the model unmarked** (A4-1). `untrusted_context_message`
  is used nowhere in `routes/email_*.py`, `src/builtin_actions.py`,
  `routes/calendar_routes.py` or `routes/document/document_routes.py`. The reply
  prompt appends past emails, contacts and attachment text to the system
  prompt. A sender's text can steer the summary, the urgency score and alert,
  the spam move, the draft reply and the learned signature. Every incoming
  email runs these prompts.
- **Compaction stores untrusted text as a system message** (A4-3). The
  summariser reads tool output, web pages and emails as plain conversation, and
  its summary becomes `role: "system"` for the rest of the chat.
- **The scheduled check-in gives an agent tools next to an unmarked data dump**
  (RSS, notes, MCP text), with instructions that contradict each other (A4-2).
- **Harness rules sit inside "do not follow instructions" envelopes** (A1-10).
  The skills index, the open-document rules, the open-email rules and a block
  labelled "Trusted instruction for this turn" are all wrapped in
  `UNTRUSTED_CONTEXT_HEADER`. A model that obeys the header ignores them; a
  model that does not weakens the header everywhere else. The skills index being
  inside it may be part of why skills are under-used.
- **The same 417-character header repeats on every envelope** (A1-11, A4-21),
  usually 3 to 6 per turn outside the cached prefix. The system policy already
  says it once.
- **Skill judges and the skill improver read skill text unmarked**, and the
  improver's output becomes SKILL.md (A4-20).

Fix direction: data goes in `untrusted_context_message` blocks with a short
header; handling rules go in harness directives or domain rules; system strings
stay static.

## 2. Harness text still pushes toward narrow, stopped work

These contradict the delegation rules added today and keep the old behaviour
alive.

- The worker `status` response tells the parent to "restart them with a
  narrower task" (A3-1, `loadout_tools.py:926`) and to "end your turn" instead
  of waiting (A3-21, `:939`).
- `_handoff_guidance` says "Do not start or resume workers" in three branches,
  and never says what a fulfilled request is or to check the worker's evidence
  (A3-3, A3-4).
- The start response says a round count "never cuts it off" and then announces
  a wrap-up at round N (A3-2). Preflight's write refusal suggests narrowing the
  task (A3-20).
- `_PARENT_CHAT_NOTE`, sent on every worker turn, gives a report shape but no
  finish line (A3-10). The person's-request block calls itself "context", which
  weakens it as the target (A3-11).
- In the base rules, "the request is the deliverable" has no completion
  criterion, and "trust success" conflicts with "check before saying done"
  (A1-4, A1-5).
- The Penpot designer loadout shipped in `docker/penpot/` caps searches and
  render fixes and uses a report shape without `Needs` lines (A3-5, A3-6).

## 3. The model does not receive the tool descriptions as written

- `compact_function_tool_schemas` keeps only the whole sentences that fit 400
  characters per tool and 160 per parameter (A2-1). 29 native tools are longer,
  so their routing guidance never ships. For example, `preview_file` loses its
  compare-before-done rule (A2-17), and `delegate_to_claude_code` is 1,277
  characters in source. The wire payload is 91.5K characters against 109.9K in
  source. Fix: a test that fails when a description exceeds the limit, so the
  source shows what ships, then rewrite the long ones.
- `manage_agent_loadout` costs about 6.3K characters per round. In the
  production log, 43 of its 49 calls were start, status or stop (A2-2).
  `manage_git` and `manage_agent_worktree` are exempt from clipping, repeat
  themselves and carry stale text (A2-5, A2-6).
- Dead ends: `grep: timed out` gives no cause or next step and discards partial
  hits (A2-3). `read_file` truncates at 20,000 characters without saying so or
  how to continue (A2-4, 285 calls). `web_fetch` refuses SVG with a hint that
  leads nowhere (A2-15). `web_search` points at `trigger_research`, which is not
  sent (A2-16). The `workspace` parameter text contradicts the refusal that
  follows, which failed 4 times in one run (A2-14).
- The top 15 tools by weight could drop from about 32.1K to 15.5K characters
  per round with no loss of guidance; the rewrites are in A2.

## 4. Always-on text that is stale, duplicated or contradictory

- Incident notes about Expo and reinstalling ride in every workspace round,
  about 1,300 characters (A1-1).
- The workspace block bans email and documents in exactly the turns where the
  harness merges those tools in, and repeats the files domain and base rules
  (A1-2, A1-6).
- Email identity and style rules are written four times and hard-code one
  user's taste (A1-13). One email block teaches header building that the
  active-email block calls wrong (A1-12).
- The fenced `bash` section forbids heredocs and then recommends one (A1-25).
- The `Needs user:` contract is stated in four places (A1-24, A3-13).
- Savings: about 3,300 characters per round on a workspace coding turn and about
  1,100 on an assistant turn.

## 5. Background prompts that disagree with their code

- The research page extractor asks for fields the pipeline discards, and its
  one-paragraph summaries drop the numbers the final report then asks for
  (A4-4).
- The skill extractor asks for commands and code that its input never contains,
  and the resulting skills are auto-published by default (A4-6).
- The document junk cleaner deletes by position from 300-character previews
  with a 200-token output cap (A4-12).
- The tier-2 turn evaluator scores a clarifying question as a failure (A4-8).
- Dead prompts: an LLM triage prompt after a `continue` (A4-9) and a 3.3K
  teacher-escalation prompt with no caller, which also carries one deployment's
  host names (A4-7).

## 6. Agent-facing docs

The Claude and Codex `odysseus` skills under `integrations/` have drifted apart
and carry a long Cookbook section with a personal host (A3-22).

## Suggested order

1. **Trust boundaries** (section 1). These are security issues on unattended
   paths.
2. **Narrowing text** (section 2). It is small, and it decides whether today's
   delegation rules take effect.
3. **Tool schemas** (section 3). Start with the length test, then the top 15
   rewrites and the dead-end results.
4. **Always-on cleanup** (section 4).
5. **Background prompts** (section 5), with the deleting cleaner first.
6. **Docs** (section 6).

`tests/test_agent_prompt_contract.py` pins several phrases these fixes touch;
the A1 fixes note which ones.
