// static/js/serverPrefs.js
//
// Account-scoped preference sync over /api/prefs/<key>.
//
// localStorage is per-browser, so a preference kept only there is lost the
// moment the same account signs in somewhere else. These helpers keep
// localStorage as the fast path for first paint while making the server copy
// the account's source of truth.
//
// Every synced preference is stored as `{ value, updated_at }` so two browsers
// can be reconciled newest-wins instead of "whichever one loaded last". A bare
// value (written before this envelope existed, or by an older build) is read as
// infinitely old, so a stamped edit from another browser always wins over it.

const ENDPOINT = '/api/prefs';
// init.js records the live username here and wipes localStorage when it
// changes. That wipe runs concurrently with the modules that read preferences,
// so between a user switch and the wipe this browser's stored copy may still
// belong to the *previous* account.
const AUTH_USER_KEY = 'odysseus-auth-user';
const _pendingWrites = new Map();
let _ownership = null;

/**
 * Whether this browser's stored preferences belong to the signed-in account.
 *
 * This decides one thing: may a local copy be pushed up as the account's? If
 * the answer were assumed yes, signing in as a second user on a browser the
 * first user had used could overwrite the second user's theme or navigation
 * order with the first user's. When ownership is in doubt the local copy is
 * read-only — the account's copy is taken instead, never replaced.
 *
 * Deployments with auth disabled have a single identity, so they answer yes.
 */
export function localCopyBelongsToAccount() {
  if (_ownership) return _ownership;
  _ownership = (async () => {
    let cached = null;
    try { cached = window.localStorage.getItem(AUTH_USER_KEY); } catch (_) { return true; }
    if (!cached) return true;  // nothing here yet — nothing to misattribute.
    try {
      const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
      if (!res.ok) return true;
      const data = await res.json().catch(() => ({}));
      const live = (data && data.username) || '';
      return !live || cached === live;
    } catch (_) {
      return true;
    }
  })();
  return _ownership;
}

/** Wrap a value with a write timestamp. */
export function stamp(value, at) {
  const when = Number(at);
  return { value, updated_at: Number.isFinite(when) && when > 0 ? when : Date.now() };
}

/** Normalize anything read from storage into an envelope, or null. */
export function envelope(raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw === 'object' && !Array.isArray(raw)
      && Object.prototype.hasOwnProperty.call(raw, 'value')
      && Object.prototype.hasOwnProperty.call(raw, 'updated_at')) {
    const when = Number(raw.updated_at);
    return { value: raw.value, updated_at: Number.isFinite(when) ? when : 0 };
  }
  return { value: raw, updated_at: 0 };
}

/** Read one preference for the signed-in account. Null when unset/unreachable. */
export async function readPref(key) {
  try {
    const res = await fetch(`${ENDPOINT}/${encodeURIComponent(key)}`, { credentials: 'same-origin' });
    if (!res.ok) return null;
    const body = await res.json();
    return envelope(body && body.value);
  } catch (_) {
    // Signed out, offline, or prefs disabled — the local copy still applies.
    return null;
  }
}

/** Queue a write. Bursts (dragging several rail icons) collapse into one PUT. */
export function writePref(key, value, at) {
  const entry = stamp(value, at);
  const queued = _pendingWrites.get(key);
  if (queued) clearTimeout(queued);
  _pendingWrites.set(key, setTimeout(() => {
    _pendingWrites.delete(key);
    try {
      fetch(`${ENDPOINT}/${encodeURIComponent(key)}`, {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: entry }),
      }).catch((e) => console.warn(`[prefs] sync failed for ${key}:`, e && e.message));
    } catch (e) {
      console.warn(`[prefs] sync error for ${key}:`, e && e.message);
    }
  }, 200));
  return entry;
}

/**
 * Decide whether this browser's copy or the account's copy of `key` wins, and
 * push the local one up when it is the newer of the two.
 *
 * Returns `{ source: 'local' | 'remote' | 'none', entry }`. A tie resolves to
 * the server so that two browsers carrying unstamped legacy values converge on
 * the account's value rather than each keeping its own.
 */
export async function reconcile(key, local) {
  const mine = (await localCopyBelongsToAccount()) ? envelope(local) : null;
  const theirs = await readPref(key);
  if (!theirs) {
    if (mine) writePref(key, mine.value, mine.updated_at);
    return { source: mine ? 'local' : 'none', entry: mine };
  }
  if (!mine) return { source: 'remote', entry: theirs };
  if (mine.updated_at > theirs.updated_at) {
    writePref(key, mine.value, mine.updated_at);
    return { source: 'local', entry: mine };
  }
  return { source: 'remote', entry: theirs };
}

export default { stamp, envelope, readPref, writePref, reconcile, localCopyBelongsToAccount };
