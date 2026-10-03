export function syncAgamemnonChatIdentity() {
  const sid = window.sessionModule?.getCurrentSessionId?.();
  const session = window.sessionModule?.getSessions?.()?.find((item) => item.id === sid);
  const title = document.getElementById('ag-session-label');
  // The empty-chat welcome already says it is a new chat; only name an
  // existing session here so the heading does not repeat that subtitle.
  if (title) title.textContent = session?.name || (sid ? 'Untitled session' : '');
}

function syncResponseStatus(event) {
  const status = event?.detail?.active ? 'Responding' : '';
  const node = document.getElementById('ag-chat-status');
  if (node) node.textContent = status;
}

// Sessions and model controls are initialized asynchronously. Capture their
// user-driven changes and the app's model-change notifications without
// coupling identity artwork to the session implementation.
document.addEventListener('change', syncAgamemnonChatIdentity);
document.addEventListener('click', syncAgamemnonChatIdentity);
window.addEventListener('focus', syncAgamemnonChatIdentity);
window.addEventListener('modelchange', syncAgamemnonChatIdentity);
window.addEventListener('sessionchange', syncAgamemnonChatIdentity);
window.addEventListener('odysseus:chat-busy-change', syncResponseStatus);
document.addEventListener('odysseus:history-rendered', syncAgamemnonChatIdentity);
syncAgamemnonChatIdentity();
