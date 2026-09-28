/* cardState.js — remembers which sub-agent cards the user opened.
 *
 * Worker cards in the chat (the live run card and the worker's handed-back
 * result) render collapsed. Opening one is a choice worth keeping across a
 * page refresh, so the keys of the OPEN cards are kept in localStorage;
 * a card with nothing stored stays collapsed. Keys are the worker's run or
 * session id, prefixed by card kind. The list is capped so it cannot grow
 * without bound; the oldest opens fall off first.
 */

const STORE_KEY = 'odysseus-agent-cards-open';
const MAX_KEYS = 300;

function _read() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
    return Array.isArray(v) ? v.filter((k) => typeof k === 'string') : [];
  } catch (_) {
    return [];
  }
}

export function isCardOpen(key) {
  return !!key && _read().includes(String(key));
}

export function setCardOpen(key, open) {
  if (!key) return;
  const k = String(key);
  const keys = _read().filter((x) => x !== k);
  if (open) keys.push(k);
  try { localStorage.setItem(STORE_KEY, JSON.stringify(keys.slice(-MAX_KEYS))); } catch (_) {}
}

export default { isCardOpen, setCardOpen };
