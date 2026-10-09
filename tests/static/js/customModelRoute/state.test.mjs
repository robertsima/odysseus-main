import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from '../_support/dom.mjs';
const dom = installDom();
const { customModelRoute } = await import('../../../../static/js/customModelRoute.js');
after(() => dom.restore());
test('custom IDs retain the explicitly selected endpoint and required route type', () => {
  let picked;
  const routes = [{id:'a',url:'http://a/v1',endpointId:'a',type:'chat'},
    {id:'b',url:'http://b/v1',endpointId:'b',type:'chat'}];
  const control = customModelRoute(routes, m => { picked = m; });
  document.body.replaceChildren(control);
  control.querySelector('select').value = 'http://b/v1';
  const input = control.querySelector('input');
  input.value = ' private/unlisted '; input.dispatchEvent(new Event('input'));
  control.querySelector('button').click();
  assert.equal(picked.model, 'private/unlisted');
  assert.equal(picked.mid, 'private/unlisted');
  assert.equal(picked.endpoint, 'http://b/v1');
  assert.equal(picked.endpointId, 'b');
  assert.equal(picked.type, 'chat');
});
test('no configured route or blank ID cannot submit an inherited empty model', () => {
  let picked = false;
  const control = customModelRoute([], () => { picked = true; });
  document.body.replaceChildren(control);
  const input = control.querySelector('input');
  input.value = 'unlisted'; input.dispatchEvent(new Event('input'));
  assert.equal(control.querySelector('button').disabled, true);
  control.querySelector('button').click(); assert.equal(picked, false);
});
