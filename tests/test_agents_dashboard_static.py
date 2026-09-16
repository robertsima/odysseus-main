"""Static regressions for the Agents dashboard's live-update behavior."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
AGENTS = (ROOT / "static/js/agentsDashboard.js").read_text(encoding="utf-8")
WORKBENCH = (ROOT / "static/js/workbench.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "static/style.css").read_text(encoding="utf-8")


def test_background_refresh_updates_regions_without_replacing_dashboard_shell():
    refresh = AGENTS.split("async function refresh()", 1)[1].split("function connect()", 1)[0]
    assert "if (state.open) updateOpenView();" in refresh
    assert "if (state.open) render();" not in refresh
    assert "function updateOpenView()" in AGENTS


def test_duration_tick_does_not_re_render_dashboard():
    tick = AGENTS.split("if (!state.tick)", 1)[1].split("export function close()", 1)[0]
    assert "data-started" in tick
    assert "render();" not in tick


def test_live_refresh_preserves_composer_draft_and_focus():
    detail = AGENTS.split("function renderDetail()", 1)[1].split("function approvalHtml", 1)[0]
    assert "draft[el.id] = el.value" in detail
    assert "focus({ preventScroll: true })" in detail
    assert "setSelectionRange" in detail


def test_workers_can_be_opened_in_workbench_for_inspection():
    assert 'data-ag="inspect-run"' in AGENTS
    assert "export async function openRun(runId, sessionId)" in WORKBENCH
    assert "openRun" in WORKBENCH.rsplit("export default", 1)[1]


def test_agents_is_a_dockable_tool_window_not_a_full_page_overlay():
    assert 'class="modal agents-dashboard hidden"' in INDEX
    assert 'class="modal-content agents-modal-content"' in INDEX
    for control in ("ag-dock-left", "ag-dock-right", "ag-maximize", "close-agents-dashboard"):
        assert f'id="{control}"' in INDEX
    assert "Modals.register(MODAL_ID" in AGENTS
    assert "Modals.injectMinimizeButton(root, MODAL_ID)" in AGENTS
    assert "makeWindowDraggable(root" in AGENTS
    assert "applyEdgeDock(root, 'left')" in AGENTS
    assert "applyEdgeDock(root, 'right')" in AGENTS


def test_agent_fleet_uses_animated_robot_personification_with_reduced_motion():
    assert "function robotHtml(agent, size = '')" in AGENTS
    assert 'class="ag-row ag-bot-card' in AGENTS
    assert 'class="ag-card-crew"' in AGENTS
    assert 'class="ag-console-hero"' in AGENTS
    assert ".ag-bot-running" in STYLE
    assert "@keyframes ag-bot-work" in STYLE
    assert ".ag-bot, .ag-bot-antenna i, .ag-card-beacon, .ag-bot-card::after { animation: none !important; }" in STYLE


def test_steering_messages_show_their_state_and_age():
    """A queued steer that silently never lands is the failure the steering
    log exists to expose, so the detail pane must show what became of each
    message — not just how many are queued."""
    detail = AGENTS.split("function renderDetail()", 1)[1].split("function steerLogHtml", 1)[0]
    assert "${steerLogHtml(r)}" in detail
    row = AGENTS.split("function steerRowHtml(m)", 1)[1].split("function approvalHtml", 1)[0]
    assert "pill(state)" in row, "state is rendered in the existing pill language"
    # Age uses the same ticking element as every other duration on the page,
    # and stops at the state the message reached.
    assert 'class="ag-row-dur" data-started=' in row
    assert "const finished = waiting ? '' : (stamps[state] || m.updated_at || '');" in row
    for state in ("queued", "acknowledged", "injected", "cancelled"):
        assert f"{state}: [" in AGENTS, "steer states reuse the STATUS pill map"


def test_the_ui_does_not_claim_a_steer_was_carried_out():
    """Nothing observes an agent acting on a steer, so the UI stops at
    `injected` and says as much instead of inventing a completed state."""
    notes = AGENTS.split("const STEER_STATE_NOTE", 1)[1].split("};", 1)[0]
    assert "not something the server can see" in notes
    assert "completed:" not in notes and "superseded:" not in notes


def test_navigation_order_module_is_loaded_by_main_app():
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert "import './js/navOrder.js?v=20260916accountprefs1';" in app


def test_live_refresh_updates_fleet_counts():
    stats = AGENTS.split("function updateStats()", 1)[1].split("function updateOpenView()", 1)[0]
    assert "box.innerHTML = triageHtml()" in stats
    assert "live.outerHTML = liveHtml()" in stats


def test_header_publishes_each_fleet_count_once():
    """The triage row and the telemetry tile strip rendered the same four
    numbers one above the other. Only the triage row remains."""
    assert "ag-telemetry" not in AGENTS
    assert "ag-telemetry" not in STYLE
    assert "function telemetryHtml" not in AGENTS
    # Counts still reach the header: filterable ones on the segment badges,
    # the rest on the single muted summary line.
    assert 'class="ag-seg' in AGENTS
    assert 'class="ag-hist"' in AGENTS


def test_applying_a_preset_keeps_its_mcp_policy_through_save():
    """profileConfig() fed saveAgentConfig(), which falls back to ['*'] when
    mcp_access is missing — a preset granting no connections used to save as
    'every connection'."""
    profile_config = AGENTS.split("function profileConfig(profile)", 1)[1].split("function loadoutSummaryHtml", 1)[0]
    assert "mcp_access: profile.mcp_access || 'all'," in profile_config
    save = AGENTS.split("async function saveAgentConfig(row)", 1)[1].split("// ── actions", 1)[0]
    assert "draft.mcp_access === 'none'" in save


def test_fleet_selection_and_window_focus_are_keyboard_accessible():
    assert 'aria-labelledby="ag-window-title"' in INDEX
    assert 'class="ag-row-name ag-card-select"' in AGENTS
    assert 'data-ag="select-agent"' in AGENTS
    assert 'role="button" tabindex="0"' not in AGENTS
    assert "root.focus({ preventScroll: true })" in AGENTS
    assert "returnFocus.focus({ preventScroll: true })" in AGENTS
    assert "const focusedUnit" in AGENTS


def test_opening_chat_or_workbench_keeps_control_room_open():
    actions = AGENTS.split("async function onClick(e)", 1)[1].split("async function sendToChat", 1)[0]
    workbench = actions.split("act === 'workbench'", 1)[1].split("act === 'refresh'", 1)[0]
    inspect_run = actions.split("act === 'inspect-run'", 1)[1].split("act === 'stop-chat'", 1)[0]
    open_chat = AGENTS.split("async function openChat(sid)", 1)[1].split("// ── open / close", 1)[0]
    assert "close()" not in workbench
    assert "close()" not in inspect_run
    assert "close()" not in open_chat


def test_selected_agent_is_introduced_once():
    """The detail pane opened with a hero *and* a meta strip: two headers for
    one agent, restating a latest-step line the fleet card and the event log
    already show, plus worker/event counts the section headers repeat."""
    detail = AGENTS.split("function renderDetail()", 1)[1].split("function approvalHtml", 1)[0]
    assert "ag-detail-head" not in detail
    assert "ag-console-eyebrow" not in detail
    assert "events.length} events" not in detail
    assert "ag-detail-head" not in STYLE
    # The single header still carries identity, runtime facts and the controls.
    assert 'class="ag-detail-name"' in detail
    assert 'class="ag-detail-meta"' in detail
    assert 'data-ag="open-chat"' in detail
    assert 'class="ag-console-actions"' in detail


def test_body_header_does_not_restate_the_window_title():
    assert "ag-title-text" not in AGENTS
    assert "ag-title" not in STYLE
    markup = AGENTS.split("surface.innerHTML = `", 1)[1].split("  if (!state.configOpen) renderDetail();", 1)[0]
    assert "Mission floor" not in markup
    # The window's own title bar is the one place the window is named.
    assert '<h3 id="ag-window-title">Agent Control Room</h3>' in INDEX


def test_workbench_shortcut_is_hidden_when_the_caller_cannot_use_it():
    """workbench.js hides its rail button on a 403; offering the shortcut
    anyway left non-admins a button whose only outcome was an error toast."""
    assert "function workbenchAvailable()" in AGENTS
    head = AGENTS.split("function render()", 1)[1].split("function filteredRows", 1)[0]
    assert "workbenchAvailable() ?" in head


def test_robot_layout_reserves_room_for_antennae_and_scaled_hero():
    assert ".ag-card-avatar { min-height: 60px" in STYLE
    assert "padding-top: 5px" in STYLE.split(".ag-bot {", 1)[1].split("}", 1)[0]
    assert 'class="ag-console-robot-bay"' in AGENTS
    assert ".ag-console-hero { position: relative; display: grid; grid-template-columns: 86px minmax(0, 1fr) auto" in STYLE
    assert ".ag-console-robot-bay { width: 86px; height: 82px" in STYLE


def test_each_agent_has_a_dedicated_server_backed_capability_loadout():
    for token in ("Agent loadout", "delegation_policy", "memory_access", "skill_access",
                  "model_access", "allowed_mcp_servers", "private_vault_access", "save-config"):
        assert token in AGENTS
    assert "'/api/agents/catalog'" in AGENTS
    assert "method: 'PATCH'" in AGENTS
    assert 'class="ag-loadout-workspace wb-card"' in AGENTS
    assert 'data-ag="config-tab"' in AGENTS
    assert 'data-config-panel="tools"' in AGENTS
    assert ".ag-config-panel-scroll" in STYLE


def test_control_room_has_one_expansion_control_and_resizable_monitor_panes():
    """Expansion lives on the title bar only. The body used to carry an
    "Expand" button running the identical snapModalToZone call."""
    assert 'data-ag="expand"' not in AGENTS
    assert 'id="ag-maximize"' in INDEX
    assert "$('ag-maximize')?.addEventListener('click', () => snapModalToZone(root" in AGENTS
    assert 'data-ag-splitter' in AGENTS
    assert "beginFleetResize" in AGENTS
    assert "odysseus-agents-fleet-width" in AGENTS
    assert ".ag-pane-splitter" in STYLE
    assert ".agents-modal-content::after" in STYLE


def test_control_room_reply_keeps_the_window_open_while_switching_chat():
    send = AGENTS.split("async function sendToChat", 1)[1].split("async function selectChat", 1)[0]
    assert "close()" not in send
    assert "await selectChat(sid)" in send


def test_a_cut_off_worker_reads_as_partial_work_not_a_failure():
    """launch_worker records `incomplete` for a run that spent its round budget
    mid-task. Neither status renderer knew the word: the dashboard fell through
    to a classless pill showing the raw status, and the Workbench to the red
    'bad' style, so a resumable partial result looked like a crash."""
    control = (ROOT / "src/agent_control.py").read_text(encoding="utf-8")
    assert '"incomplete"' in control, "precondition: the backend still emits this status"
    assert "incomplete: ['Out of rounds', 'warn']" in AGENTS
    assert "s === 'incomplete'" in WORKBENCH
    workbench_class = WORKBENCH.split("function statusClass(status)", 1)[1].split("\n}", 1)[0]
    assert "'incomplete'" in workbench_class.split("return 'warn'", 1)[0]
