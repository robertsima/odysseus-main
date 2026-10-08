/** Place the ONE interactive session title in the selected composition.
 * Session updates, export, rename, focus and future title features keep the
 * same element and handlers; the appearance owns only its location. */
export function placeChatTitle(style) {
  const title = document.getElementById('current-meta');
  const host = style === 'agamemnon'
    ? document.getElementById('ag-session-label')
    : document.querySelector('.chat-meta-overlay');
  if (title && host && title.parentElement !== host) host.prepend(title);
}
