# Harness reliability and efficiency — 2026-09-09

## Evidence

Recent live chat logs showed a read-only Vault Mind request selecting 30 native tools (2,916 compact-schema tokens) and spending four planning rounds before producing a report. The same logs showed repeated Chroma `404 Not Found` checks for absent legacy collections alongside a deliberately unavailable HTTP embedding endpoint. Those checks add network work and warning noise without improving retrieval.

## Priorities

1. Route read-only Vault Mind requests to semantic search plus a safe cited-file reader rather than the full local-workspace toolset. Vault mutations retain the existing file workflow. This reduces schema cost without weakening private-directory path controls.
2. Cache only confirmed missing *legacy* Chroma collections for five minutes. Do not cache connectivity, authentication, or other operational errors.
3. Keep per-round usage accounting as the next iteration: it requires a migration and a durable write boundary, so it is deferred rather than rushed into this reliability patch.

## Acceptance criteria

- Read-only Vault queries offer `search_documents` and `read_file`, but not the Terminus toolset solely because the vault was named.
- Vault mutation requests still route to the complete existing file workflow.
- Repeated absent legacy-collection checks produce one Chroma lookup within the TTL; connection failures retry.
- Existing privacy protections continue to govern reads of private Journal paths.
- Focused retrieval, embedding, context, and startup-import tests pass.

## Post-redeployment exercise — 2026-09-16

The 19:59–20:06 logs show preparation below one second and strong cache reuse
within a turn, but 21–33 seconds before first response text across four/five
model rounds. A notification request selected the email suite; repeated worker
launches hit the one-worker limit; loadout listing and API discovery added large
results; a launch-only audit produced an automatically learned skill. The final
calendar answer may reflect a mid-turn steer, which the old logs did not identify.

Implemented acceptance criteria:

- Notification/agent-message wording cannot seed email schemas through generic
  verbs. Email implementation audits select source tools, while reading mail
  about software and explicit result delivery remain valid email requests.
- Empty chat settings apply the existing worker limit. Queued CLI jobs count
  immediately; polling and cancellation remain available at capacity. Provider
  failures and unconfirmed starts remain distinguishable from completed work.
- Discovery results are bounded and pageable, with no duplicate endpoint table.
  Loadout discovery is compact and full configuration remains retrievable.
- Steering badges disappear when delivered and instructions remain normal chat
  messages. Cancelled/failed chips clear with durable history retained.
  Stream resume and chat switches do not leave stale labels or
  render a late response into another chat.
- A human steer adds relevant permitted tools before the next model request and
  retains unfinished user objectives unless superseded. Synthetic context and
  peer messages do not become the human retrieval query.
- Skill extraction skips launch-only or unsuccessful evidence and supplies
  bounded outcome metadata to the teacher, without additional raw tool contents
  or arguments. Learned guidance is not called proven.
- Slow email-read logs separate connection acquisition, folder selection and
  message fetch durations instead of labeling cumulative timings as phases.

Deployment validation still requires replaying the same user flows against the
configured providers. Unit and simulated-stream regressions verify routing,
capacity, context bounds and delivery behavior; they do not establish a new live
latency figure. The observed slow IMAP revalidations already run in the existing
cache/revalidation path; no provider-latency reduction is claimed here.

## Eight-concurrent-jobs exercise — 2026-09-16, 22:07–22:16

The new logs contain established-chat requests answered only with "Understood"
or "How can I help?", followed by successful repeats in fresh chats. They also
show explicit "using agents" requests missing the delegation toolset, a parent
worker limit of one despite provider capacity being configured to eight, and a
documentation lookup becoming an automatically learned skill. Steering was
logged as injected during a worker poll; that is evidence of delivery, not proof
that another agent stole it or that the UI retained its message.

Implemented regression coverage and acceptance criteria:

- [x] Agent prompt construction places trailing synthetic retrieval/date
  envelopes before the latest genuine human request, preserving assistant/tool
  ordering. Test the actual chat-context builder's output, not just a hand-made
  prompt. This removes one context-ordering failure path; it cannot guarantee
  that a provider never returns a generic acknowledgement.
- [x] Action-scoped "using agents" instructions select delegation tools.
  Source audits of email implementations do not seed live-mailbox tools merely
  because the subject contains "email". Existing execution-time permissions
  remain authoritative.
- [x] Steering has session-and-run isolation, stable preparation/headless
  targets, durable human-message identity and redraw reconciliation. No peer
  message may be promoted to a human instruction. No later run adopts a stale
  or unbound correction.
- [x] Per-agent worker ceilings and provider-wide capacity are labeled
  separately and capacity responses identify the limiting scope. No existing
  chat is silently granted additional delegation permission.
- [x] Post-response extraction snapshots its originating conversation and
  respects memory-write access. Peer/runtime/retrieval data are not treated as
  human memory evidence; lookup-only or unsuccessful work is not automatically
  learned as a procedure.
- [x] Fleet monitoring is compact and paginated. Detail sections are separate
  views; loadout editing does not share a long scrolling inspector with logs.
  Open chat and Stop are directly available in Control Room and Workbench.
- [x] Agent cleanup is recoverable archive/restore plus hiding terminal child
  cards. It preserves history, rejects live/queued work and checks ownership.

Deployment follow-up / TODO:

Local validation: 866 regression tests passed; JavaScript syntax and diff checks
passed. Browser QA used real UI assets with synthetic fleet data, including
agent-switch draft preservation and a docked editor whose Behavior controls fit
without scrolling. A separate provider-integration suite returned 119 passed,
1 skipped and 15 failures; an isolated export of committed `4c4ce59` reproduced
the same 15 Windows/POSIX environment failures. These are not live-provider
results. That slice was subsequently pushed to `dev` in `7817436`; deployment
and live-provider acceptance remain operator checks.

- [ ] Repeat same-chat documentation → mailbox → source-audit flows with the
  configured provider, then compare selected tools and actual results.
- [ ] With provider capacity eight, explicitly set the test parent's child
  allowance to eight. Start independent workers and steer the parent while a
  worker poll is outstanding. Verify the ordinary human bubble survives both
  a history refresh and switching away/back; inspect its recorded target/state.
- [ ] Stop a run during preparation and while tools are active. Verify pending
  steers become cancelled, not delivered to the next request.
- [ ] Archive an idle test agent, restart, and restore it. Confirm active
  descendants block archive and that child-card cleanup retains run history.
- [ ] Compare first-response latency, tool/schema count and extraction traffic
  with the same prompts. Local mocked-stream and synthetic browser checks do
  not establish production performance or validate eight live provider jobs.

## Specialist research / MCP routing audit follow-up

- [x] Recognize "use appropriately scoped agents" as explicit delegation while
  keeping ordinary questions, negation and injected skill text non-authorizing.
- [x] Expose `orchestrate_agents` for scoped research fan-out and optional synthesis,
  using existing workers, parent capacity, ownership checks and policy narrowing.
- [x] Select bounded read-only tools from explicitly named connected MCP servers,
  independent of their always-bound catalog-size budget. Persist positive per-agent
  tool bindings and enforce them against late-connected or hand-written calls.
- [x] Distinguish loaded skills from execution; handle batch dependency activation.
- [x] Record real child IDs, attached tools, calls, attempts, handoffs and synthesis
  outcome. Stop/timeout/failure preserve traceable partial results.
- [x] Guard unsupported orchestration completion claims, and recover failed
  Deep Research synthesis once without repeating the research searches.
- [x] Require successful observations from bound web/MCP research tools; merely
  attaching tools or loading skills is not evidence of completed research.
- [x] Preserve narrow positive tool policies with large MCP catalogs, honor
  equivalent email tool names, and fail closed on permission-read/write errors.
- [x] Release capacity on pre-start cancellation, retain handoffs after activity
  registry eviction, and persist interrupted workflows without replaying them.

Local validation: 1,870 passed and 7 skipped in the broad agent/MCP/tool/skill/
research regression selection. The remaining 27 failures were reproduced on an
isolated archive of unchanged `7e771528`: Windows path/symlink/subprocess-environment
fixtures and stale embedding/document assertions. The focused workflow/routing/
recovery/dashboard/approval selection passed all 209 tests. Fresh-interpreter
imports of the dispatcher, registry and agent loop passed, as did compilation
and diff checks. Live-provider validation remains an operator check.

Deployment / live acceptance TODO (mocked-model tests are not live evidence):

- [ ] In a permitted parent chat, request buyer/problem, competitor/positioning and
  content research agents, followed by synthesis producing a market map and ten
  draft posts. Load the operator's named marketing/handoff skills.
- [ ] Confirm three research child chats plus synthesis in Agents/Workbench;
  each must show actual attached tools and calls, with separate handoff artifact IDs.
- [ ] Bind only Bluesky `get-profile`, `get-timeline`, `get-post`, `get-posts`
  qualified tool names to content research; verify an actual read call, no posting
  schemas/actions, and explicit partial status if Bluesky cannot provide evidence.
- [ ] Repeat with parent worker limit one, then multiple workers; verify queueing,
  stop/timeout, result collection and `continue` without duplicate launches.
- [ ] Reproduce a synthesis-provider failure and verify preserved findings plus
  `outcome=partial`, not a silently successful incomplete report.
