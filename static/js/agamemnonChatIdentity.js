// Only the decorative busy label is appearance-specific. The session title
// is the shared interactive #current-meta (placed by chatHeader.js).

function syncResponseStatus(event) {
  const status = event?.detail?.active ? 'Responding' : '';
  const node = document.getElementById('ag-chat-status');
  if (node) node.textContent = status;
}

window.addEventListener('odysseus:chat-busy-change', syncResponseStatus);
