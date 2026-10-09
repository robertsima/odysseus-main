// Five decorative effects share one bounded, time-based canvas lifecycle.
export const ATMOSPHERE_PATTERNS = ['fog', 'waves', 'fireflies', 'snowfall', 'ripples'];

export function startAtmosphere(pattern) {
  const canvas = document.createElement('canvas');
  canvas.id = `${pattern}-canvas`;
  canvas.className = 'bg-atmosphere-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  const ctx = canvas.getContext('2d');
  if (!ctx) return () => {};
  document.body.prepend(canvas);
  let width, height, frame, elapsed = 0, last = null, stopped = false;
  const particles = Array.from({ length: 48 }, (_, i) => ({
    x: Math.random(), y: Math.random(), phase: Math.random() * Math.PI * 2,
    speed: 0.4 + Math.random() * 0.6, cycle: i / 48,
  }));
  function resize() {
    width = window.innerWidth; height = window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function stop() {
    stopped = true;
    cancelAnimationFrame(frame);
    window.removeEventListener('resize', resize);
    document.removeEventListener('visibilitychange', visibility);
    canvas.remove();
  }
  function visibility() {
    cancelAnimationFrame(frame);
    last = null;
    if (!document.hidden && !stopped) frame = requestAnimationFrame(draw);
  }
  function glow(x, y, radius, alpha, color, stretch = 1) {
    ctx.save();
    ctx.translate(x, y); ctx.scale(1, stretch);
    const gradient = ctx.createRadialGradient(0, 0, 0, 0, 0, radius);
    gradient.addColorStop(0, color); gradient.addColorStop(1, 'transparent');
    ctx.fillStyle = gradient; ctx.globalAlpha = alpha;
    ctx.beginPath(); ctx.arc(0, 0, radius, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }
  function draw(now) {
    if (stopped) return;
    if (!canvas.isConnected) { stop(); return; }
    if (last !== null) elapsed += Math.min((now - last) / 1000, 0.05);
    last = now;
    const styles = getComputedStyle(document.documentElement);
    const color = styles.getPropertyValue('--bg-effect-color').trim()
      || styles.getPropertyValue('--fg').trim() || '#9cdef2';
    const size = Math.max(0.2, Math.min(3, parseFloat(styles.getPropertyValue('--bg-effect-size')) || 1));
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = color; ctx.strokeStyle = color;
    if (pattern === 'fog') {
      for (const p of particles.slice(0, 9)) {
        const x = ((p.x + elapsed * 0.012 * p.speed) % 1.6 - 0.3) * width;
        const y = (p.y + Math.sin(elapsed * 0.15 + p.phase) * 0.08) * height;
        glow(x, y, Math.max(width, height) * 0.36 * size, 0.14, color, 0.48);
      }
    } else if (pattern === 'waves') {
      ctx.lineWidth = 1.5 * size;
      for (let band = 0; band < 8; band++) {
        ctx.globalAlpha = 0.09 + band * 0.012;
        ctx.beginPath();
        for (let x = 0; x <= width + 12; x += 12) {
          const y = height * (0.2 + band * 0.09)
            + Math.sin(x / (180 * size) + elapsed * 0.35 + band * 0.6) * 30 * size;
          if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
    } else if (pattern === 'ripples') {
      ctx.lineWidth = 1.2 * size;
      for (const p of particles.slice(0, 12)) {
        const age = (elapsed * 0.12 + p.cycle * 4) % 1;
        ctx.globalAlpha = Math.sin(age * Math.PI) * 0.22;
        ctx.beginPath();
        ctx.arc(p.x * width, p.y * height, (8 + age * 140) * size, 0, Math.PI * 2);
        ctx.stroke();
      }
    } else {
      for (const p of particles) {
        const snow = pattern === 'snowfall';
        const x = (p.x * width + Math.sin(elapsed * 0.4 + p.phase) * (snow ? 25 : 50) * size + width) % width;
        const y = snow ? ((p.y * height + elapsed * 25 * p.speed) % (height + 20)) - 10
          : p.y * height + Math.cos(elapsed * 0.3 + p.phase) * 30 * size;
        if (snow) {
          ctx.globalAlpha = 0.18 + p.speed * 0.25;
          ctx.beginPath(); ctx.arc(x, y, (1 + p.speed * 2) * size, 0, Math.PI * 2); ctx.fill();
        } else {
          const alpha = 0.12 + (Math.sin(elapsed * p.speed + p.phase) + 1) * 0.2;
          glow(x, y, 10 * size, alpha, color);
          ctx.globalAlpha = alpha;
          ctx.beginPath(); ctx.arc(x, y, 1.4 * size, 0, Math.PI * 2); ctx.fill();
        }
      }
    }
    ctx.globalAlpha = 1;
    frame = requestAnimationFrame(draw);
  }
  resize();
  window.addEventListener('resize', resize);
  document.addEventListener('visibilitychange', visibility);
  if (!document.hidden) frame = requestAnimationFrame(draw);
  return stop;
}
