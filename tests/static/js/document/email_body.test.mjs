// The rich body of an email draft in static/js/document.js. A draft's body
// comes from the model or from a received message, so the HTML it carries is
// untrusted; the editor shows it through the same raw-HTML sanitizer as chat
// markdown, or a handler in the mail runs in the user's session.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

document.body.innerHTML = '<div id="chat-container"><div id="chat-history"></div></div>';
const documentModule = (await import('../../../../static/js/document.js')).default;

test('an email draft with HTML in its body renders without event handlers or script URLs', async () => {
  const body = '<p>Hello <b>there</b> <img src="x.png" onerror="alert(1)"></p>' +
    '<p><a href="javascript:alert(2)" onclick="alert(3)">open</a></p>';
  documentModule.injectFreshDoc({
    id: 'doc-email-1',
    title: 'Re: hello',
    language: 'email',
    content: `To: bob@example.com\nSubject: Re: hello\n---\n${body}`,
  });

  const rich = () => document.getElementById('doc-email-richbody');
  await waitFor(() => rich() && rich().querySelector('b'), { what: 'the email body to render' });

  const nodes = [...rich().querySelectorAll('*')];
  const handlers = nodes.flatMap((el) =>
    [...el.attributes].filter((a) => a.name.startsWith('on')).map((a) => `${el.tagName}[${a.name}]`));
  assert.deepEqual(handlers, []);
  assert.equal(rich().querySelector('a').getAttribute('href'), null);
  assert.equal(rich().querySelector('b').textContent, 'there', 'the formatting itself is kept');
});
