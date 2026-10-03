/** A completed agent round's server-measured wall time (seconds). */
export function roundDurationLabel(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const whole = Math.round(seconds);
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor(whole % 3600 / 60)}m`;
}

export function showRoundDuration(wrap, seconds) {
  const label = roundDurationLabel(seconds);
  if (!wrap || !label) return;
  const host = wrap.classList.contains('agent-thread') ? wrap : wrap.querySelector('.role');
  if (!host) return;
  let badge = host.querySelector(':scope > .agent-round-duration');
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'agent-round-duration';
    badge.title = 'Completed agent round · wall time including tools and waits';
    host.appendChild(badge);
  }
  badge.textContent = `Round · ${label}`;
}
