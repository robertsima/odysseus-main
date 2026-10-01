from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_model_precedes_execution_source():
    identity = (ROOT / "static/js/agamemnonIdentity.js").read_text()
    assert "return matchIdentity(model) || matchIdentity(source)" in identity
    assert "['anthropic', /claude|anthropic/, '#d97757']" in identity
    assert "['openai', /gpt|openai|o[134]" in identity


def test_model_color_is_applied_only_to_agents_control_room_soldiers():
    index = (ROOT / "static/index.html").read_text()
    agents = (ROOT / "static/js/agentsDashboard.js").read_text()
    workbench = (ROOT / "static/js/workbench.js").read_text()
    chat_identity = (ROOT / "static/js/agamemnonChatIdentity.js").read_text()
    assert 'class="ag-soldier-sprite"' in agents
    assert 'href="${agentMarksUrl()}#soldier-${soldierVariant}"' in agents
    assert 'data-soldier-variant="${soldierVariant}"' in agents
    assert "resolveAgamemnonModelIdentity" in agents
    assert "--agent-model-color:${modelIdentity.color}" in agents
    assert 'data-model-family="${modelIdentity.family}"' in agents
    assert 'ag-chat-agent-mark' not in index
    assert 'ag-run-agent-mark' not in index
    assert "applyAgamemnonModelIdentity" not in workbench
    assert "applyAgamemnonModelIdentity" not in chat_identity
    assert "window.sessionModule?.getCurrentModel?.()" in chat_identity


def test_brand_helmet_and_control_room_soldier_are_distinct_vector_artworks():
    helmet = (ROOT / "static/branding/agamemnon-trojan-helmet.svg").read_text()
    marks = (ROOT / "static/branding/agamemnon-agent-marks.svg").read_text()
    assert "side-profile Trojan helmet" in helmet
    assert "game-icons:spartan-helmet by Delapouite" in helmet
    for variant in ("primary", "worker", "scout", "reviewer", "specialist"):
        assert f'id="soldier-{variant}"' in marks
    assert "Exact Game Icons vectors, CC BY 3.0" in marks
    assert '<use href="#soldier-primary"/>' in marks
    helmet_path = helmet.split(' d="', 1)[1].split('"', 1)[0]
    soldier_path = marks.split(' d="', 1)[1].split('"', 1)[0]
    assert helmet_path != soldier_path
