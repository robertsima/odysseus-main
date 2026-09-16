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
