# Upstream sync, 2026-09-18: what changed and what still has to be re-ported

This sync redoes the upstream merge that PR #25 attempted (reverted in `ab232825`).
The chosen foundation is **upstream's agent core**: `src/agent_loop.py` and
`src/tool_approvals.py` are upstream's, and the fork's work on top of them is
re-ported feature by feature in follow-up PRs. Everything else is a real
three-way merge, including `src/tool_execution.py` and
`src/agent_tools/filesystem_tools.py`. Those two carry the fork's security
boundaries, so taking upstream's copies would have removed protections rather
than features.

## Kept in this PR

These are enforced at execution time, so they hold whatever the loop offers
the model.

- **Private vault boundary** (`da53f2c4`):
  - Documents labelled private are denied to every file tool: read, write,
    edit, patch, ls, glob and grep.
  - `bash`, `python` and MCP filesystem tools are refused without the chat's
    private-vault grant.
  - The grant flows from the route through `stream_agent_loop(allow_private=...)`
    into `execute_tool_block`, where it is re-checked against fresh session
    settings. `allow_private` and `steer_run_id` (below) are the only
    additions to upstream's loop signature.
- **Protected paths** (`7f3edbd3`, `8a7e6dc2`): the file tools refuse:
  - the agent worktree's approval state, so an agent cannot forge a publish grant,
  - private key files, by extension,
  - the configured signing key.
- **Capability-profile policy**: session settings are re-read on every tool
  call, so a profile or tool change takes effect mid-turn.
- **Lotus privacy**: MCP Lotus calls from an endpoint the owner has not
  approved are refused.
- **Rounds never end a run** (`331c996c`). This is ported into upstream's loop:
  `max_rounds` is advisory and the loop-breaker bounds a run. The fork's
  `MAX_AGENT_ROUNDS = 0` would otherwise have given upstream's
  `range(1, max_rounds + 1)` zero rounds.
- **Steer and agent-to-agent message delivery** (`faf3ee0a`, `a5322720`):
  - Upstream's loop now drains the steer queue at the top of every round.
  - The queue is keyed by `steer_run_id`, which the chat route and headless
    workers pass in.
  - A peer agent's message goes in with its own attribution; the user's is
    labelled as a mid-task instruction.
  - Without this, `message_agent` and the Steer box queued messages that
    nothing ever read.
- **Native tool calls over the ChatGPT Responses API** (`bd83989e`). Nothing
  needed porting: `llm_core` emits the same `tool_calls` event upstream's loop
  reads.
- **ChatGPT/Codex models get tool schemas**: `chatgpt.com` is in upstream's
  `_API_HOSTS`. Without it these models were sent no tools and waited for
  prose tool blocks they never write.
- **The request is the last thing the model reads** (`78174368`): the chat
  builder appends retrieval and time context after the human turn for prompt
  caching, and upstream's prompt builder now moves that trailing context back
  in front of the request.
- **Bundled skills don't arm the approval gate**:
  - Upstream wraps skill text as untrusted and arms the exact-approval gate
    whenever skills are shown. The fork seeds bundled skills on every install,
    so that would gate every turn.
  - The gate is skipped only when every skill shown is unmodified shipped
    content (`src/builtin_skills.is_shipped_skill`). Checking the
    `source: bundled` label isn't enough, because the seeder keeps an edited
    body.
  - Any skill a user or agent wrote or edited still arms the gate.
- **Explicit capability classes for the fork's tools** in
  `src/tool_capabilities.py`, following upstream's closest equivalents.
  `manage_git`'s history rewrites and discards are marked destructive.
- **Workspace fallback keeps its refusals**: the personal-docs second chance in
  `_resolve_tool_path` applies only to paths that are merely outside the
  workspace. It no longer masks an application-state or sensitive-path
  refusal.
- **Exact `skills.sh/<owner>/<repo>/<skill>` links** map to GitHub without a
  network fetch (fork). Other skills.sh links take upstream's checked
  redirect path.
- **Pure helpers moved out of the fork's loop**:
  - `src/delegation_intent.py` holds the delegation recogniser. Workflow
    launch authorization uses it.
  - `src/skill_toolsets.py` resolves a skill's `requires_toolsets`. The
    manage_skills author warning uses it.

## Re-ported since

- **Approval modes** (`a3a153e3`, `3c8c2654`). `src/approval_modes.py` now
  drives upstream's gate through `ToolRunSecurityContext.approval_mode`:
  - `auto` never asks, `ask_risky` asks for destructive and outward-facing
    calls, and `ask_all` asks for every change and keeps the untrusted-context
    gate as well.
  - The chat route, headless sub-agents, background-job follow-ups and
    scheduled tasks pass the chat's mode, else the app default
    (`agent_approval_mode`). A run with no mode (the skill tester, teacher
    escalation, API-token runs) keeps upstream's untrusted-context gate.
  - A mode approval uses upstream's exact-action card. It carries no
    untrusted context, so the executor accepts it in an unarmed run.
  - A sub-agent's card is saved in its own chat and listed in the Agents
    panel, with the reason it asked.
- **Execution ledger and tool-output offload** (`6131859d`, `ddb518ef`).
  Both modules had survived the sync, but nothing in upstream's loop called
  them. As a result, every tool result stayed in the history in full: a
  GitHub research turn grew to 700k tokens. The per-round trim then dropped
  whole results, the agent re-read what it had lost, and each round was a
  full-prompt cache miss. Now:
  - A formatted result over the inline limit is offloaded where the loop
    formats it. This matters most for MCP output, which has no size cap of
    its own.
  - `_append_tool_results` collapses completed exchanges in batches.
  - `tests/test_execution_ledger.py::test_the_agent_loop_actually_calls_it`
    runs again.
- **Named files select the files domain** (`bd83989e`, `98a4e3ff`). A
  request that names a file with a known extension (`notes.md`, `app.ts`), a
  path (`/app/data/...`, `~/projects/x/`) or the vault by any of its names
  (vault, Obsidian, knowledge base, AI Mind) seeds the `files` domain in
  `_classify_agent_request` again. Without it "fix the bug in app.ts" was
  offered no file tool. The rest of the fork's vault routing (seeding
  `search_documents`) is still on the backlog.
  `tests/test_agent_files_domain_filenames.py` runs in full again.

## Added to upstream's loop since

- **Terminus toolset retention** (`f8882905`, `3e2a0eb5`, `5181684e`).
  `apply_terminus_toolset()` merges the local-machine toolset into the turn's
  selection instead of replacing it whenever the user's own words also named
  an assistant domain, and on the replace path it still carries across
  whatever retrieval matched for this query (MCP tools included). The
  `on|from <bare word>` branch of `_LOCAL_COMPUTER_REFERENCE_RE` now requires
  the token to look like a host, so "confirmations from Gmail" is no longer
  read as work targeted at a machine called Gmail. `audit_emails` was also
  missing from the `email` domain map, leaving deterministic seeding with no
  path to the whole-mailbox report tool.

  Without these the scheduled "Applied Job Status Tracker" run ended with
  "the email-audit tool/schema is not available in this run" (2026-09-19
  logs). Covered by `tests/test_terminus_toolset_retention.py`. The rest of
  `tests/test_agent_self_blocking_toolsets.py` (missing-tool re-arm,
  starved-domain repair) is still on the backlog; see "Backlog tests removed
  2026-10-03" below to restore it.

- **Tool budget** (`agent_tool_budget`, default 40). Keyword domain seeding
  can offer most of the catalogue on a long message (80 tools, ~66k prompt
  tokens in the 2026-09-18 logs). Past the budget,
  `_apply_tool_budget` drops whole seeded domains, starting with the ones
  retrieval agrees with least. Retrieved, forced, document, upload and
  skill-required tools always stay.
- **Result-aware loop-breaker**. A repeated call counts toward the stall
  streak only if its result is unchanged too (`_result_progress_digest`; a
  tool can name its own `progress_key`). A polled Claude Code job that shows
  new activity is progress, and one that shows only a larger elapsed time is
  not. Repeat polls also wait on the job (30s, doubling, capped at 240s)
  instead of returning at once.

- **Same-turn tool attachment** (2026-09-22). `discover_tools` works again:
  the loop builds a `TurnToolDiscovery` over native and MCP schemas, hands it
  to the executor, and attaches what it loads to the next round
  (`continue_same_turn: true`). It is always offered, including on
  caller-provided selections. Its allowance (32 tools / 4,096 estimated
  schema tokens a turn) covers only what discovery loads: schemas the round
  already sends no longer spend it. On 2026-09-24 they had, on every busy
  chat, so each call returned `budget_limited`, even for the exact
  `mcp__…__get_profile` the prompt listed. A deterministic, exact-name form of the
  missing-tool re-arm: a short final answer that says it lacks a named,
  permitted tool gets that tool attached and one more round, at most twice a
  turn (`_missing_tools_to_attach`). Bare names (`get_profile` for
  `mcp__c5ec6d7a__get_profile`) are deliberately not resolved: a bare name
  does not say which server, and after a failed call "list_projects isn't
  available" is about the service, not the schema. The fork's prose-shape
  detectors and starved-domain repair are still on the backlog. Covered by
  `tests/test_same_turn_tool_attachment.py`.
- **Skill routing through `skill_declared_tools`**. Matched skills, a skill
  loaded with `manage_skills view`, and a profile's selected skills resolve
  `requires_toolsets` by exact name, MCP server name, or alias, instead of a
  bare known-name match that dropped `todoist`, `lotus` and server names. A
  chat's `skill_access`/`skill_names` now scope the skill index and matched
  procedures, and a profile's selected skills bind their tools from round one.
- **Per-agent profile instructions** (`0ea6b80b`, `f2f9f83e`). A worker
  chat's `agent_instructions` are appended to the system prompt after the
  platform contract, bounded and labelled as unable to override it, and kept
  out of the cached base prompt (`_scoped_agent_customization`).
- **Offer matches enforcement**. The loop applies the chat's saved tool
  policy (`session_policy_disabled_tools`) whichever caller started the
  turn, and adds tools the user names outright.

- **Carrying a request through** (2026-09-29). Not a re-port of the fork's
  continuation or targeted self-unblock; new mechanisms on upstream's loop,
  prompted by 15 Lead Engineer runs that delivered 3 fixes for one request:
  a turn that ran tools and stops short is asked once to clear the blocker or
  name it (`_reports_blocked`, `Needs user:` / `Needs parent:`); a per-chat
  task checklist (`src/task_checklist.py`, written by `update_plan` and
  `todowrite`) is shown on later turns and holds open a turn that left its steps
  open; a worker hand-back may send the same worker back within
  `agent_auto_continue_limit` follow-ups per request
  (`agent_control._continue_parent`), which the `explicit` delegation gate no
  longer closes; an approved publish continues the chat that asked. See
  website/agent-runtime.md, "Stopping short", "The task checklist" and
  "Carrying a request through". Covered by `tests/test_agent_continuity.py`,
  `tests/test_worker_completion_no_runaway.py` and the e2e scenario
  `continue_blocked`.

## Changed behaviour until re-ported

| Area | Now | Fork commits to re-port |
|---|---|---|
| Fork approval store (once/always grants, reissue, precheck before hold) | Replaced by upstream's `ToolApprovalStore`. The Agents overview lists pending approvals and links to the chat, where the card is decided. | `a3a153e3`, `3d8ed0fc`, `8927810b` |
| `manage_git` risky actions | No per-call confirmation of its own. It is classified as a workspace write with network and external side effects (destructive for rewrites and discards), so upstream's exact-approval gate holds it once a run is tainted. Pushing this repository is still refused (`use_publish_flow`). | `3c8c2654`, `3d8ed0fc` |
| Approval prompts from skills (runs with no approval mode only) | A turn that shows any skill a user or agent wrote or edited arms upstream's gate, so the next high-impact call (bash, writes) asks for an exact approval. "Allow for this chat session" covers the rest of that chat. | Upstream design, kept |
| Tool routing: intent classes, domain routing, targeted self-unblock, protected admission budget, missing-tool re-arm, starved-domain repair | Upstream's selection | `8927810b`, `c62b6bac`, `cffc5f0d`, `d97fa0e7`, `32614be0`, `a654a09e`, `3e2a0eb5`, `f8882905`, `fc74bf51`, `5ee56c0a` |
| Continuation ("ok, continue" keeps the last turn's tools) | Upstream's handling | `8927810b`, `00b35dbd`, `a364f8f2` |
| Delegation in the loop: policy gating, standing `delegation_granted`, plain-language "start an agent" routing | Workflows still check authorization. The loop does not hide or route delegation tools. | `ced11e62`, `7c9c03a2`, `16ad4e47`, `0ea6b80b` |
| Steering beyond delivery: tools added because of a steer, and continuing a turn that would end while a steer is pending | Steers and peer-agent messages are delivered between rounds (see Kept). A steer that arrives after the last round is dropped with a visible `steer_dropped` event. | `75988e56`, `4c4ce597` |
| Prompt and schema efficiency: stable prefix, schema ledger, cache-shard affinity, reasoning replay, context accounting | Upstream's | `97b6691c`, `913a605d`, `f4bdeed2`, `c129bbcc`, `8cca5a1e`, `d47e5160` |
| Schema-level hiding: private-grant tools, Lotus, loadout-disallowed tools | Offered to the model but refused at execution | `da53f2c4`, `0ea6b80b`, `cabbe6d1` |
| Git, research and MCP routing prompts | Upstream's, except server-mention read tools, now re-ported: a message naming a connected MCP server attaches up to 8 of its read-only tools (`McpManager.discover_requested_tools`; never writes), kept out of the tool budget's reach, and such a message no longer takes the tool-free first-turn reply. The word "browser" does not name the builtin browser, whose expansion would attach its whole catalogue, writes included (`tests/test_requested_mcp_routing.py`) | `e16d1ec5`, `94fa0d17`, `982e60eb`, `9f7062d0`, `836eb7d8` |
| Knowledge-base and vault routing | Upstream's | `68646700`, `8dfacd08`, `4360acf2`, `98a4e3ff` |
| Request self-heal, `site:` handling | Upstream's | `0611fa1e` |

The fork tests for these features were skipped with a `Re-port backlog:`
reason and then deleted on 2026-10-03, including
`tests/test_agent_loop_fork.py`, the fork's whole loop suite
(`tests/test_agent_loop.py` is upstream's). Each follow-up PR restores the
tests for the feature it brings back, using the table below.

Found along the way, not changed here: the bundled-skill seeder rewrites each
installed `SKILL.md` through `Skill.to_markdown()`, which drops some section
headings and the free-form body text (`body_extra`).

## Backlog tests removed 2026-10-03

Decision D13 of `website/testing-restructure-2026-10-03.md` deleted the
skipped `Re-port backlog:` tests: 8 files skipped at module level (206 tests)
and 119 tests skipped one by one in 25 other files, 325 test functions in all.
`tests/test_capability_tool_gating.py` had no other tests, so it went too.
Every file below is unchanged at `d2df387e` on `dev`. To bring a feature's tests back, run the command in the last column
and copy out the tests that cover the feature. For a partly removed file, copy
only the named tests and the helpers they use.

| File | Tests removed | Restore with |
|---|---|---|
| `tests/test_agent_context_accounting.py` | Whole file, 6 tests | `git show d2df387e:tests/test_agent_context_accounting.py` |
| `tests/test_agent_intent_dev_requests.py` | Whole file, 18 tests | `git show d2df387e:tests/test_agent_intent_dev_requests.py` |
| `tests/test_agent_latest_user_skips_context.py` | Whole file, 8 tests | `git show d2df387e:tests/test_agent_latest_user_skips_context.py` |
| `tests/test_agent_loop_fork.py` | Whole file, 73 tests | `git show d2df387e:tests/test_agent_loop_fork.py` |
| `tests/test_agent_self_blocking_toolsets.py` | Whole file, 60 tests | `git show d2df387e:tests/test_agent_self_blocking_toolsets.py` |
| `tests/test_agent_steering_routing.py` | Whole file, 3 tests | `git show d2df387e:tests/test_agent_steering_routing.py` |
| `tests/test_agent_targeted_rearm.py` | Whole file, 22 tests | `git show d2df387e:tests/test_agent_targeted_rearm.py` |
| `tests/test_capability_tool_gating.py` | Whole file, 14 tests | `git show d2df387e:tests/test_capability_tool_gating.py` |
| `tests/test_knowledge_base_reach.py` | Whole file, 16 tests | `git show d2df387e:tests/test_knowledge_base_reach.py` |
| `tests/test_agent_log_hygiene.py` | `test_tool_set_diff_line_is_info_only_when_the_set_changes`, `test_an_unchanged_tool_name_logs_at_debug`, `test_image_generation_off_disables_generate_image_in_the_selection` | `git show d2df387e:tests/test_agent_log_hygiene.py` |
| `tests/test_agent_self_unblock_budget.py` | `test_recovery_paths_admit_as_named_requests_not_guesses`, `test_a_refused_late_addition_is_logged_not_silent`, `test_persisted_tool_events_count_as_tools_the_conversation_used`, `test_only_the_previous_tool_using_turn_is_continued`, `test_known_names_filter_what_is_continued`, `test_ok_continue_resumes_with_the_tool_the_last_turn_was_using` | `git show d2df387e:tests/test_agent_self_unblock_budget.py` |
| `tests/test_agent_tool_routing.py` | `test_keyword_fallback_matches_the_index_pass_exactly`, `test_keyword_fallback_does_not_fire_on_substrings` | `git show d2df387e:tests/test_agent_tool_routing.py` |
| `tests/test_agent_turn_lifecycle.py` | `TestSteerIsObservable::test_persisted_steering_keeps_human_and_peer_attribution` | `git show d2df387e:tests/test_agent_turn_lifecycle.py` |
| `tests/test_agents_dashboard_routes.py` | `test_agent_routing_uses_the_human_request_not_injected_context`, `test_overview_is_owner_scoped_and_grouped` | `git show d2df387e:tests/test_agents_dashboard_routes.py` |
| `tests/test_chat_route_tool_policy.py` | `test_semantic_browser_hit_does_not_expand_the_whole_server`, `test_explicit_browser_intent_still_expands_connected_tools`, `test_conversation_tool_retention_still_defers_to_route_policy` | `git show d2df387e:tests/test_chat_route_tool_policy.py` |
| `tests/test_chat_settings_routes.py` | `test_settings_and_approval_routes` | `git show d2df387e:tests/test_chat_settings_routes.py` |
| `tests/test_claude_code_consolidation.py` | `test_agent_loop_tool_ceiling_is_500_and_zero_disables` | `git show d2df387e:tests/test_claude_code_consolidation.py` |
| `tests/test_git_native_arguments.py` | `test_one_use_approval_equates_sparse_and_provider_expanded_calls_only`, `test_legacy_repo_pull_approval_equates_neutral_fillers_but_not_targets`, `test_one_use_approval_never_equates_meaningful_target_changes` | `git show d2df387e:tests/test_git_native_arguments.py` |
| `tests/test_git_tool_failures.py` | `test_an_approved_call_is_returned_exactly_until_it_runs`, `test_a_denied_or_foreign_approval_is_never_handed_back`, `test_a_truncated_command_is_not_offered_because_it_could_never_match`, `test_the_loop_prechecks_before_it_holds`, `test_a_retired_approval_is_not_handed_back`, `test_running_an_approved_but_invalid_call_retires_its_approval` | `git show d2df387e:tests/test_git_tool_failures.py` |
| `tests/test_harness_routing_integration.py` | `test_direct_greeting_path_applies_only_that_sessions_persona`, `test_initial_schemas_match_effective_private_execution_grant`, `test_local_git_sync_is_bound_without_semantic_hit_or_private_grant`, `test_pasted_git_https_auth_failure_binds_diagnostic_tool`, `test_generic_github_login_questions_are_not_local_git_diagnostics`, `test_git_push_emits_confirmation_before_execution_even_in_auto`, `test_complete_round_selection_is_exact_and_stably_sorted`, `test_selected_base_prompt_does_not_reexpand_all_admin_tools`, `test_selected_mcp_prompt_is_bounded_and_native_prompt_has_no_duplicate`, `test_native_round_one_discovers_and_round_two_attaches_and_executes`, `test_fenced_round_two_prompt_contains_discovered_tool_signature`, `test_fenced_dynamic_mcp_discovery_attaches_parses_and_dispatches`, `test_small_connected_mcp_is_deferred_on_unrelated_turn_but_discoverable`, `test_global_revocation_removes_next_round_schema_and_blocks_stale_call`, `test_runtime_profile_ceiling_controls_discovery`, `test_concurrent_live_loops_isolate_round_two_and_preserve_shared_caller_set` | `git show d2df387e:tests/test_harness_routing_integration.py` |
| `tests/test_history_display_model_hydration.py` | `test_model_send_routes_hydrate_before_context_build` | `git show d2df387e:tests/test_history_display_model_hydration.py` |
| `tests/test_intent_assessment.py` | `test_agent_compatibility_classifier_preserves_domain_free_discovery`, `test_agent_classifier_keeps_file_intent_when_url_grounds_git_request`, `test_agent_classifier_does_not_turn_mcp_use_feedback_into_settings_admin` | `git show d2df387e:tests/test_intent_assessment.py` |
| `tests/test_kv_cache_invalidation_2927.py` | `test_agent_cached_prefix_hash_ignores_turn_tail_and_detects_real_prefix_changes`, `test_agent_history_prefix_continuity_detects_mid_history_mutation` | `git show d2df387e:tests/test_kv_cache_invalidation_2927.py` |
| `tests/test_lotus_builtin.py` | `test_lotus_private_filter_follows_owner_access_policy` | `git show d2df387e:tests/test_lotus_builtin.py` |
| `tests/test_lotus_notifications.py` | `test_wellbeing_tool_is_wired_into_the_agent_surfaces` | `git show d2df387e:tests/test_lotus_notifications.py` |
| `tests/test_manage_git.py` | `test_routine_ask_all_once_grant_is_consumed_by_handler`, `test_risky_action_requires_session_exact_once_grant_and_revision_proof`, `test_grant_is_not_canonicalized_across_whitespace_in_path`, `test_manage_git_always_never_authorizes_a_future_push`, `test_manage_git_always_covers_local_history_work`, `test_risky_approval_overrides_auto_and_fenced_routing_is_exact`, `test_history_rewrites_always_require_exact_call_confirmation` | `git show d2df387e:tests/test_manage_git.py` |
| `tests/test_mcp_tool_binding.py` | `test_connected_external_mcp_tools_survive_a_rag_miss`, `test_admin_intent_no_longer_hides_the_real_tools_behind_manage_mcp`, `test_large_embedded_catalogs_still_require_retrieval_relevance`, `test_gated_tool_shows_once_actually_retrieved`, `test_disabled_external_tool_stays_hidden_even_though_unconditionally_bound`, `test_tool_availability_is_stable_across_rounds_regardless_of_retry_state`, `test_mcp_mgr_none_yields_no_mcp_schemas_without_raising`, `test_oversized_server_is_demoted_while_its_small_peers_stay_bound`, `test_many_small_servers_are_trimmed_largest_first_to_the_total_cap`, `test_demoted_tool_is_still_reachable_once_retrieval_surfaces_it`, `test_demoted_server_stays_listed_in_the_prompt_with_a_way_back`, `test_agent_debug_line_names_the_demoted_servers`, `test_the_note_speaks_the_phrase_the_self_unblock_listens_for` | `git show d2df387e:tests/test_mcp_tool_binding.py` |
| `tests/test_prompt_injection_audit.py` | `test_per_agent_customization_is_scoped_and_security_precedes_it` | `git show d2df387e:tests/test_prompt_injection_audit.py` |
| `tests/test_repository_local.py` | `test_expanded_merge_preserves_required_empty_ref` | `git show d2df387e:tests/test_repository_local.py` |
| `tests/test_repository_sync_tool.py` | `test_repository_approval_distinguishes_inspection_from_pull` | `git show d2df387e:tests/test_repository_sync_tool.py` |
| `tests/test_research_workflow_routing.py` | `test_exact_user_wording_routes_to_real_launcher_and_authorizes_delegation`, `test_information_requests_or_agent_tools_and_tests_do_not_authorize_delegation`, `test_genuine_human_continuation_retains_explicit_delegation`, `test_new_information_request_does_not_inherit_old_launch_authority`, `test_worker_tool_and_runtime_text_cannot_grant_delegation_on_continue`, `test_real_loop_exposes_workflow_launcher_and_passes_delegation_authority`, `test_real_loop_retains_human_continuation_authority_ignoring_peer_text`, `test_continue_uses_durable_workflow_receipt_without_nudging_duplicate_start`, `test_continue_cannot_adopt_another_owners_or_chats_durable_receipt`, `test_new_explicit_request_cannot_claim_older_completed_workflow_as_current_execution`, `test_ordinary_question_cannot_start_workflows_even_if_retrieval_selected_launcher`, `test_zero_tool_calls_suppress_long_false_completion_and_return_deterministic_not_run`, `test_running_workflow_receipt_cannot_be_presented_as_completed`, `test_completed_wait_receipt_replaces_running_receipt_and_allows_answer`, `test_loading_skill_procedure_does_not_count_as_execution`, `test_lifecycle_request_never_authorizes_or_nudges_new_launch`, `test_loop_does_not_call_model_when_policy_storage_fails`, `test_restart_after_a_failed_run_still_authorizes_specialists`, `test_real_prohibitions_and_workflow_controls_still_refuse`, `test_skill_toolsets_resolve_mcp_server_names_and_flag_only_real_prose`, `test_a_resolvable_toolset_switched_off_is_not_reported_as_bad_metadata`, `test_scoped_workers_do_not_warn_about_domains_they_were_never_given`, `test_delegation_recognisers_stay_linear_on_hostile_text`, `test_delegation_authorization_is_standing_for_the_chat_not_per_message`, `test_a_chat_that_never_asked_for_agents_is_still_refused`, `test_failing_to_persist_the_grant_does_not_refuse_the_turn`, `test_a_round_budget_does_not_truncate_a_run_that_is_still_working` | `git show d2df387e:tests/test_research_workflow_routing.py` |
| `tests/test_task_run_now_foreground.py` | `test_placeholder_delta_does_not_mask_a_stream_error`, `test_placeholder_alone_fails_the_run` | `git show d2df387e:tests/test_task_run_now_foreground.py` |
| `tests/test_workspace_confine.py` | `test_workspace_coding_request_surfaces_only_permitted_edit_and_verify_tools` | `git show d2df387e:tests/test_workspace_confine.py` |
