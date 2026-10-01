from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_model_precedes_execution_source():
    identity = (ROOT / "static/js/agamemnonIdentity.js").read_text()
    assert "return matchIdentity(model) || matchIdentity(source)" in identity
    assert "['anthropic', /claude|anthropic/, '#d97757']" in identity
    assert "['openai', /gpt|openai|o[134]" in identity


def test_all_production_identity_surfaces_use_trojan_mark():
    index = (ROOT / "static/index.html").read_text()
    agents = (ROOT / "static/js/agentsDashboard.js").read_text()
    workbench = (ROOT / "static/js/workbench.js").read_text()
    chat_identity = (ROOT / "static/js/agamemnonChatIdentity.js").read_text()
    assert 'id="ag-chat-agent-mark"' in index
    assert 'id="ag-run-agent-mark"' in index
    assert 'class="ag-trojan-mark"' in agents
    assert "applyAgamemnonModelIdentity($('ag-run-agent-mark')" in workbench
    assert "applyAgamemnonModelIdentity(document.getElementById('ag-chat-agent-mark'), model)" in chat_identity
    assert "window.sessionModule?.getCurrentModel?.()" in chat_identity
    assert "window.addEventListener('modelchange', syncAgamemnonChatIdentity)" in chat_identity


def test_sprite_reuses_exact_penpot_crest_and_has_direct_preview():
    crest = (ROOT / "static/branding/agamemnon-hoplitic-crest.svg").read_text()
    marks = (ROOT / "static/branding/agamemnon-agent-marks.svg").read_text()
    path = crest.split('<path fill="#d7b35a" d="', 1)[1].split('"', 1)[0]
    assert path in marks
    assert '<use href="#trojan"/>' in marks
