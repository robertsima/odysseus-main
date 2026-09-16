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


def test_navigation_order_module_is_loaded_by_main_app():
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    assert "import './js/navOrder.js?v=20260915controlroom1';" in app


def test_live_refresh_updates_fleet_telemetry():
    stats = AGENTS.split("function updateStats()", 1)[1].split("function updateOpenView()", 1)[0]
    assert "telemetry.innerHTML = telemetryHtml()" in stats


def test_fleet_selection_and_window_focus_are_keyboard_accessible():
    assert 'aria-labelledby="ag-window-title"' in INDEX
    assert 'class="ag-row-name ag-card-select"' in AGENTS
    assert 'data-ag="select-agent"' in AGENTS
    assert 'role="button" tabindex="0"' not in AGENTS
    assert "root.focus({ preventScroll: true })" in AGENTS
    assert "returnFocus.focus({ preventScroll: true })" in AGENTS
    assert "const focusedUnit" in AGENTS
