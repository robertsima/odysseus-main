# Auto-name failure backoff

## Problem

Session auto-naming runs as a background task after each response. When the
selected model or endpoint is unavailable, overlapping responses can issue
duplicate requests and each later response immediately retries the same known
failure. This increases token/API cost and floods logs without improving the
session title.

## Behavior

- Allow at most one in-flight auto-name request per session in this process.
- After an exception or an unusable title, wait five minutes before retrying.
- Clear retry state after a valid title is saved.
- Lazily remove expired entries so the process-local map remains bounded.
- Keep auto-naming best-effort; it must not interfere with chat responses.

## Verification

Tests cover concurrent deduplication, failure backoff, cooldown expiry, and the
existing successful endpoint/model fallback path.
