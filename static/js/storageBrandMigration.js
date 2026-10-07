// Upgrade bridge for installed UI extensions and older tabs. Only branded
// preference keys are mapped; unrelated origin storage is untouched.
export function canonicalKey(key) {
  return String(key).replace(/^odysseus(?=[.\-_A-Z]|$)/, 'agamemnon');
}

export function installStorageBrandMigration(storage, prototype) {
  if (!storage || !prototype || prototype.__agamemnonBrandBridge) return;
  const get = prototype.getItem;
  const set = prototype.setItem;
  const remove = prototype.removeItem;
  const legacyKey = key => String(key).replace(/^agamemnon(?=[.\-_A-Z]|$)/, 'odysseus');
  prototype.getItem = function(key) {
    if (this !== storage) return get.call(this, key);
    const canonical = canonicalKey(key);
    const value = get.call(this, canonical);
    if (value !== null) return value;
    const old = get.call(this, legacyKey(canonical));
    if (old !== null && canonical !== legacyKey(canonical)) {
      try { set.call(this, canonical, old); } catch (_) { /* Read works even at quota. */ }
    }
    return old;
  };
  prototype.setItem = function(key, value) {
    if (this !== storage) return set.call(this, key, value);
    const canonical = canonicalKey(key);
    set.call(this, canonical, value);
    // Older tabs/extensions must see the same preference during the upgrade.
    const legacy = legacyKey(canonical);
    if (legacy !== canonical) {
      try { set.call(this, legacy, value); } catch (_) { /* Canonical write succeeded. */ }
    }
  };
  prototype.removeItem = function(key) {
    if (this !== storage) return remove.call(this, key);
    const canonical = canonicalKey(key);
    remove.call(this, canonical);
    if (legacyKey(canonical) !== canonical) remove.call(this, legacyKey(canonical));
  };
  Object.defineProperty(prototype, '__agamemnonBrandBridge', {value: true});
}

try {
  if (typeof window !== 'undefined' && typeof Storage !== 'undefined') {
    installStorageBrandMigration(window.localStorage, Storage.prototype);
  }
} catch (_) { /* Storage denied: existing callers keep their own fallbacks. */ }
