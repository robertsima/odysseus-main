"""The knowledge base must be reachable as an index AND as files, at once.

Three failures used to compound into "the vault feels fragmented":

  1. Naming the vault routed the turn to the file/shell (Terminus) toolset,
     which has no `search_documents`, and the prompt rules are derived from the
     selected tool names -- so the turn arrived with grep/glob/ls and a rule
     pack that says "prefer grep, glob and ls". It did.
  2. `search_documents` cites the real path of every chunk, but binding a
     workspace switched path confinement to workspace-only, so the agent was
     handed a path its own `read_file` then refused.
  3. The only way to reach a writable copy of the vault was to bind it as the
     workspace, which in turn revoked everything else.
"""
import os
import tempfile

import pytest

from src.agent_loop import (
    _DOMAIN_RULES,
    _DOMAIN_TOOL_MAP,
    _KNOWLEDGE_BASE_TOOLS,
    _VAULT_READ_TOOLS,
    _is_read_only_vault_request,
    _domain_rules_for_tools,
    _looks_like_vault_request,
    apply_terminus_toolset,
)


# ── 1. a vault turn keeps the semantic index ────────────────────────────────

@pytest.mark.parametrize("text", [
    "Edit the appropriate job report in AI Mind",
    "update the rolling report in my vault",
    "what does Vault Mind say about the deploy process",
    "check my obsidian knowledge base for the NAS notes",
])
def test_vault_phrasing_is_recognised(text):
    assert _looks_like_vault_request(text)


def test_terminus_swap_does_not_carry_search_documents_on_its_own():
    """Guards the premise: the file toolset alone has no retrieval tool, which
    is why the seed below has to exist. If Terminus ever gains it, this test
    fails loudly rather than leaving a redundant seed in place."""
    swapped = apply_terminus_toolset(set(), query_matched=set(), domains={"files"})
    assert "search_documents" not in swapped


def test_knowledge_base_seed_survives_the_terminus_swap():
    selected = apply_terminus_toolset(
        {"search_documents"}, query_matched=set(), domains={"files"}
    )
    # The swap drops it (that is the bug) ...
    assert "search_documents" not in selected
    # ... and the deterministic seed, applied after, puts it back.
    selected |= set(_KNOWLEDGE_BASE_TOOLS)
    assert "search_documents" in selected


def test_knowledge_base_rules_travel_with_the_retrieval_tool():
    rules = "\n".join(_domain_rules_for_tools({"search_documents", "read_file", "grep"}))
    assert "## Knowledge base rules" in rules
    assert "search_documents" in rules
    assert "do not need a workspace" in rules.lower()


def test_knowledge_base_rules_are_absent_without_the_tool():
    rules = "\n".join(_domain_rules_for_tools({"read_file", "grep", "bash"}))
    assert "## Knowledge base rules" not in rules


def test_every_domain_in_the_map_has_rules():
    assert set(_DOMAIN_TOOL_MAP) <= set(_DOMAIN_RULES)


# ── 2 + 3. one path works in both modes ─────────────────────────────────────

@pytest.fixture
def vault(monkeypatch):
    """A personal-docs tree standing in for PERSONAL_DIR."""
    root = os.path.realpath(tempfile.mkdtemp())
    notes = os.path.join(root, "AI Mind")
    os.makedirs(notes)
    with open(os.path.join(notes, "report.md"), "w", encoding="utf-8") as f:
        f.write("# report\n")
    monkeypatch.setattr("src.constants.PERSONAL_DIR", root, raising=False)
    return root


@pytest.fixture
def workspace():
    d = os.path.realpath(tempfile.mkdtemp())
    with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    return d


@pytest.fixture
def bound(workspace):
    from src.tool_execution import _active_workspace

    token = _active_workspace.set(workspace)
    yield workspace
    _active_workspace.reset(token)


def test_vault_path_resolves_with_a_workspace_bound(vault, bound):
    from src.tool_execution import _resolve_tool_path

    cited = os.path.join(vault, "AI Mind", "report.md")
    assert _resolve_tool_path(cited) == os.path.realpath(cited)


def test_workspace_still_resolves_and_still_confines(vault, bound):
    from src.tool_execution import _resolve_tool_path

    assert _resolve_tool_path("main.py") == os.path.realpath(
        os.path.join(bound, "main.py")
    )
    outside = os.path.realpath(tempfile.mkdtemp())
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(outside, "secrets.txt"))


def test_private_vault_directory_stays_closed_even_with_a_workspace(vault, bound, monkeypatch):
    """The private label is what protects the Journal, and the second-chance
    resolver must not be a way around it."""
    from src.tool_execution import _resolve_tool_path

    journal = os.path.join(vault, "Journal")
    os.makedirs(journal, exist_ok=True)
    monkeypatch.setattr(
        "src.rag_sensitivity.private_directories", lambda: [os.path.realpath(journal)]
    )
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(journal, "2026-09-03.md"))


def test_sensitive_names_stay_closed_even_inside_the_vault(vault, bound):
    from src.tool_execution import _resolve_tool_path

    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(vault, ".ssh", "id_rsa"))


def test_grep_root_reaches_the_vault_with_a_workspace_bound(vault, bound):
    from src.tool_execution import _resolve_search_root

    notes = os.path.join(vault, "AI Mind")
    assert _resolve_search_root(notes) == os.path.realpath(notes)
    # An empty path still means "the workspace", not "the vault".
    assert _resolve_search_root("") == os.path.realpath(bound)


# ── deployment-declared extra roots ─────────────────────────────────────────

def test_extra_roots_can_be_declared_by_the_environment(monkeypatch):
    from src.tool_execution import TOOL_EXTRA_ROOTS_ENV, _resolve_tool_path, _tool_path_roots

    extra = os.path.realpath(tempfile.mkdtemp())
    monkeypatch.setenv(TOOL_EXTRA_ROOTS_ENV, extra)
    assert extra in _tool_path_roots()
    target = os.path.join(extra, "note.md")
    assert _resolve_tool_path(target) == os.path.realpath(target)


def test_extra_roots_accept_several_separators(monkeypatch):
    from src.tool_execution import TOOL_EXTRA_ROOTS_ENV, _tool_path_roots

    a = os.path.realpath(tempfile.mkdtemp())
    b = os.path.realpath(tempfile.mkdtemp())
    monkeypatch.setenv(TOOL_EXTRA_ROOTS_ENV, f"{a},{b}")
    roots = _tool_path_roots()
    assert a in roots and b in roots


def test_extra_roots_do_not_defeat_the_sensitive_deny_list(monkeypatch):
    from src.tool_execution import TOOL_EXTRA_ROOTS_ENV, _resolve_tool_path

    extra = os.path.realpath(tempfile.mkdtemp())
    monkeypatch.setenv(TOOL_EXTRA_ROOTS_ENV, extra)
    with pytest.raises(ValueError):
        _resolve_tool_path(os.path.join(extra, ".ssh", "authorized_keys"))


def test_read_only_vault_request_uses_only_retrieval_and_safe_file_read_tools():
    assert _is_read_only_vault_request("what does AI Mind say about the deploy process")
    assert _VAULT_READ_TOOLS == {"search_documents", "read_file"}


def test_vault_mutation_keeps_the_full_file_workflow_available():
    assert not _is_read_only_vault_request("update the deployment report in AI Mind")
