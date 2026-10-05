// Put the real Settings modal from static/index.html into the test document,
// so settings.js finds every control its panels bind on open.
import { readFileSync } from 'node:fs';

const INDEX_HTML = new URL('../../../../static/index.html', import.meta.url);

export function mountSettingsModal() {
  const page = new DOMParser().parseFromString(readFileSync(INDEX_HTML, 'utf8'), 'text/html');
  const modal = document.importNode(page.getElementById('settings-modal'), true);
  document.body.append(modal);
  return modal;
}
