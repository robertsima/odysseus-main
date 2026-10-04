// The whole app shell for node tests of static/app.js: static/index.html's
// body (without its script tags) in a happy-dom page, the fetch fake, and the
// real app.js booted on top. Use it for behavior that only exists once app.js
// has wired the page (the new-chat buttons, the composer's key handling).
// Each test file gets one page; booting takes a second or two.
//
//   const page = await openAppPage({ routes: (fake) => fake.route('GET', '/api/sessions', () => [...]) });
//   document.getElementById('rail-new-session').click();
import fs from 'node:fs';
import { after } from 'node:test';

import { installDom, waitFor } from '../js/_support/dom.mjs';
import { installFetchFake } from '../js/_support/fetchFake.mjs';

export { waitFor };

const INDEX = new URL('../../../static/index.html', import.meta.url);

function pageBody() {
  const html = fs.readFileSync(INDEX, 'utf8');
  const body = html.match(/<body[^>]*>([\s\S]*)<\/body>/i)[1];
  return body.replace(/<script\b[\s\S]*?<\/script>/gi, '');
}

export async function openAppPage({ routes = () => {} } = {}) {
  const dom = installDom();
  const fake = installFetchFake();
  after(async () => {
    fake.restore();
    await dom.restore();
  });

  // Browser APIs happy-dom lacks or would send to the network.
  globalThis.EventSource = class EventSource {
    constructor(url) { this.url = url; }
    addEventListener() {}
    removeEventListener() {}
    close() {}
  };
  navigator.sendBeacon = () => true;
  HTMLCanvasElement.prototype.getContext = function getContext() {
    return new Proxy({}, {
      get: (target, key) => (key in target ? target[key] : () => {}),
      set: (target, key, value) => { target[key] = value; return true; },
    });
  };

  routes(fake);
  document.body.innerHTML = pageBody();
  await import('../../../static/app.js');
  const sessionModule = window.sessionModule;
  await waitFor(() => fake.calls.some((c) => c.url.pathname === '/api/sessions'), { what: 'the session list request', timeout: 15000 });
  await new Promise((resolve) => setTimeout(resolve, 50));
  return { fake, sessionModule };
}
