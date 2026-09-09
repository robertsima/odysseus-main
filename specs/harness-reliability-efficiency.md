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
