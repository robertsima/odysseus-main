import { applyAgamemnonModelIdentity } from './agamemnonIdentity.js';

function currentModel() {
  return window.sessionModule?.getCurrentModel?.() || '';
}

export function syncAgamemnonChatIdentity() {
  const model = currentModel();
  applyAgamemnonModelIdentity(document.getElementById('ag-chat-agent-mark'), model);
  const label = document.getElementById('ag-chat-model');
  if (label) label.textContent = model ? String(model).split('/').pop() : 'Default';
}

// Sessions and model controls are initialized asynchronously. Capture their
// user-driven changes and the app's model-change notifications without
// coupling identity artwork to the session implementation.
document.addEventListener('change', syncAgamemnonChatIdentity);
document.addEventListener('click', syncAgamemnonChatIdentity);
window.addEventListener('focus', syncAgamemnonChatIdentity);
window.addEventListener('modelchange', syncAgamemnonChatIdentity);
window.addEventListener('sessionchange', syncAgamemnonChatIdentity);
syncAgamemnonChatIdentity();
