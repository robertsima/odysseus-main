"""What the agent prompt says (src/agent_loop.py), after the 2026-09-30 sweep.

Each test pins a behaviour, not wording: which blocks appear for which tools,
and that the guidance the harness depends on is present exactly once.
"""

import re

import src.agent_loop as al
from src import task_checklist


def _system_text(relevant_tools, **kwargs):
    messages, _ = al._build_system_prompt(
        [{"role": "user", "content": "summarize my inbox"}],
        model="test", active_document=None, mcp_mgr=None, disabled_tools=set(),
        relevant_tools=set(relevant_tools), compact=True, suppress_skills=True,
        **kwargs,
    )
    return "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")


def test_the_machine_note_needs_file_or_shell_tools():
    # ask_user and update_plan are on every turn; they used to add a note that
    # the user "referred to this computer" and should not get email or notes.
    plain = _system_text({"ask_user", "update_plan", "list_emails", "read_email"})
    assert "Machine work without a workspace" not in plain
    assert "Do not use personal-assistant tools" not in plain
    shell = _system_text({"ask_user", "update_plan", "bash", "read_file"})
    assert "Machine work without a workspace" in shell


def test_a_workspace_gets_coding_mode_not_the_machine_note():
    text = _system_text({"bash", "read_file"}, workspace="/srv/project")
    assert "Workspace coding mode" in text
    assert "Machine work without a workspace" not in text


def test_delegation_guidance_comes_with_the_launch_tools():
    assert "## Delegating to workers" in _system_text({"manage_agent_loadout", "ask_user"})
    assert "## Delegating to workers" not in _system_text({"list_emails", "ask_user"})
    # Not a tool-selection domain: launchers must only arrive through their gates.
    assert "delegation" not in al._DOMAIN_TOOL_MAP


def test_the_native_prompt_names_odysseus_and_its_working_rules_once():
    text = al._assemble_prompt({"bash", "ask_user", "update_plan"}, set(), compact=True)
    assert text.startswith("You are Odysseus")
    assert text.count("## How to work") == 1
    assert "discover_tools" in text  # a missing tool is discovered, not reported missing
    assert "Before ending your turn, read your last paragraph" in text


def test_the_overridden_v1_rules_are_gone():
    source = open(al.__file__, encoding="utf-8").read()
    assert "BIAS TOWARD ACTION" not in source
    for name in ("_AGENT_PREAMBLE", "_AGENT_RULES", "_API_AGENT_RULES"):
        assert len(re.findall(rf"(?m)^{name} = ", source)) == 1, name


def test_a_worker_is_asked_for_a_report_in_a_fixed_shape():
    note = al._PARENT_CHAT_NOTE
    assert note.startswith("You were started by another chat")
    for field in ("Outcome:", "Changed:", "Checked:", "Open:", "Needs parent:", "Needs user:"):
        assert field in note


def test_the_checklist_nudge_offers_needs_parent_only_to_a_worker():
    plan = "- [x] read the failing test\n- [ ] fix it"
    top = task_checklist.continue_directive(plan)
    worker = task_checklist.continue_directive(plan, has_parent=True)
    assert "Needs user:" in top and "Needs parent:" not in top
    assert "Needs parent:" in worker and "Needs user:" in worker
    assert "- [ ] fix it" in worker


def test_draft_skills_are_not_called_authoritative():
    source = open(al.__file__, encoding="utf-8").read()
    assert "treat them as authoritative" not in source
    assert "proven to work. Follow them step by step" not in source


def test_coding_rules_send_visual_work_to_real_artwork_and_the_user():
    rules = al._workspace_coding_rules("/srv/project")
    assert "visual-asset-sourcing" in rules and "hand-written SVG" in rules
    assert "ask the user to pick" in rules


def test_delegation_rules_say_run_tests_yourself_and_review_once_per_iteration():
    assert "Run tests and builds yourself" in al._DELEGATION_RULES
    assert "one independent review per iteration" in al._DELEGATION_RULES


def test_visual_asset_sourcing_skill_parses_and_is_cross_linked():
    from pathlib import Path
    from services.memory.skill_format import Skill

    root = Path(al.__file__).resolve().parents[1] / "skills" / "design"
    text = (root / "visual-asset-sourcing" / "SKILL.md").read_text(encoding="utf-8")
    skill = Skill.from_markdown(text)
    # The portable schema nests category/status/source under `metadata:`; the
    # seeder (src/builtin_skills.py) copies them onto the skill record.
    assert skill.name == "visual-asset-sourcing" and skill.description
    head = text.split("---")[1]
    for line in ("  category: design", "  status: published", "  source: bundled"):
        assert line in head
    assert "Needs user:" in text and "api.iconify.design" in text
    penpot = (root / "penpot-design-workflow" / "SKILL.md").read_text(encoding="utf-8")
    assert "visual-asset-sourcing" in penpot and "error screen" in penpot
