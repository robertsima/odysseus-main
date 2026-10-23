// A small copy of the sidebar and icon rail markup for the navigation tests:
// the three built-in group containers with their rows, the per-tool rail
// buttons, the Email section's unread dot and the rail's group container.
// Every row and rail button records its clicks in `clicks`, standing in for the
// listeners app.js installs.
import { NAV_ITEMS, BUILTIN_GROUPS } from '../../../../static/js/navLayout.js';

export function buildNavDom(doc = document) {
  const clicks = [];
  const rail = NAV_ITEMS.filter((item) => item.rail)
    .map((item) => `<button class="icon-rail-btn" id="${item.rail}" title="${item.label}"><svg></svg></button>`).join('');
  const email = '<div class="section list-item nav-email-row" id="email-section" title="Email"><div class="section-header-flex"><span class="section-title" id="email-section-title"><svg></svg><span>Email</span></span>'
    + '<button type="button" id="email-compose-btn">new</button><span id="email-unread-dot" class="sidebar-notif-dot" style="display:none"></span></div></div>';
  const row = (item) => item.key === 'email' ? email : `<div class="list-item" id="${item.sidebar}" title="${item.label}"><svg data-icon="${item.key}"></svg><span class="grow">${item.label}</span>`
    + (item.key === 'tasks' ? '<span id="assistant-notif-dot" class="sidebar-notif-dot" style="display:none"></span>' : '')
    + (item.key === 'agents' ? '<span id="tool-agents-dot" class="sidebar-notif-dot" style="display:none"></span>' : '')
    + '</div>';
  const group = (entry) => `<div class="section nav-group" id="${entry.id}-section" data-nav-group="${entry.id}"><div class="section-header-flex"><span class="section-title nav-group-title">${entry.label}</span></div>`
    + NAV_ITEMS.filter((item) => item.group === entry.id && item.sidebar).map(row).join('') + '</div>';
  doc.body.innerHTML = `<div id="icon-rail" class="icon-rail"><button id="rail-search-btn"></button><div id="rail-nav-groups" class="rail-nav-groups"></div>`
    + `<button id="rail-chats" class="rail-dynamic" style="display:none"></button>${rail}<div style="flex:1"></div><button id="rail-settings"></button></div>`
    + `<nav id="sidebar"><div class="sidebar-inner">`
    + `${BUILTIN_GROUPS.map(group).join('')}<div id="after-groups"></div></div></nav>`;
  for (const node of doc.querySelectorAll('[id^="tool-"], [id^="rail-"], #email-section-title, #email-compose-btn')) {
    if (node.id === 'rail-nav-groups') continue;
    node.addEventListener('click', (event) => { if (event.target === node || node.contains(event.target)) clicks.push(node.id); });
  }
  return { clicks };
}

export const rowIds = (doc, groupId) => Array.from(doc.querySelectorAll(`.nav-group[data-nav-group="${groupId}"] > .list-item`)).map((node) => node.id);
export const groupIds = (doc) => Array.from(doc.querySelectorAll('.nav-group[data-nav-group]')).map((node) => node.dataset.navGroup);
