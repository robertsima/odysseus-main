// Running an HTML code block (static/js/codeRunner.js) opens it in a new
// window and writes the model's code into it. That code is untrusted: if the
// popup still holds window.opener, it can navigate the Odysseus tab
// (window.opener.location = ...) to a phishing page. The opener is cut before
// the first write.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(async () => { await dom.restore(); });

const { runHTML } = await import('../../../../static/js/codeRunner.js');

function fakePopup() {
  const popup = { opener: { name: 'the Odysseus tab' }, written: [], openerAtWrite: undefined };
  popup.document = {
    open() {},
    write(code) { popup.openerAtWrite = popup.opener; popup.written.push(code); },
    close() {},
  };
  return popup;
}

test('the popup has no opener when the code is written into it', () => {
  const popup = fakePopup();
  window.open = () => popup;
  const panel = document.createElement('div');

  runHTML('<script>opener.location="https://evil.test"</script>', panel);

  assert.equal(popup.written.length, 1);
  assert.equal(popup.openerAtWrite, null);
  assert.equal(popup.opener, null);
});

test('a blocked popup is reported in the panel and nothing is written', () => {
  window.open = () => null;
  const panel = document.createElement('div');

  runHTML('<p>hi</p>', panel);

  assert.match(panel.textContent, /Popup blocked/);
});
