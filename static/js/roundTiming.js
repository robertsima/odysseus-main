/** Format server-measured wall time in seconds. */
export function durationLabel(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const whole = Math.round(seconds);
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor(whole % 3600 / 60)}m`;
}

/** Add completed round durations into one elapsed time for the whole turn. */
export function totalTurnDuration(rounds) {
  if (!Array.isArray(rounds)) return null;
  const completed = rounds.filter((seconds) =>
    typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0
  );
  return completed.length
    ? Math.round(completed.reduce((sum, seconds) => sum + seconds, 0) * 100) / 100
    : null;
}

export function showTurnDuration(wrap, seconds) {
  const label = durationLabel(seconds);
  if (!wrap || !label) return;
  const host = wrap.classList.contains('agent-thread') ? wrap : wrap.querySelector('.role');
  if (!host) return;
  let badge = host.querySelector(':scope > .agent-turn-duration');
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'agent-turn-duration';
    badge.title = 'Total completed turn wall time, including tools and waits';
    host.appendChild(badge);
  }
  badge.textContent = `Turn · ${label}`;
}
