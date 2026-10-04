"""Keep the actual rendered artwork coupled to its attributed source.

External SVG <use> may silently disappear in a file preview or restrictive
browser. Same-document use references and generated symbols avoid that trap.
"""
import base64
from pathlib import Path
import re
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NS = '{http://www.w3.org/2000/svg}'
ROLES = ('primary', 'worker', 'scout', 'reviewer', 'specialist')
START = '<!-- AGAMEMNON SOLDIER SYMBOLS START (generated) -->'
END = '<!-- AGAMEMNON SOLDIER SYMBOLS END -->'


def test_embedded_soldiers_match_canonical_licensed_paths():
    source = ET.parse(ROOT / 'static/branding/agamemnon-agent-marks.svg').getroot()
    canonical = {s.get('id'): s.find(f'{NS}path').get('d') for s in source.findall(f'{NS}symbol') if s.get('id') in {f'soldier-{r}' for r in ROLES}}
    assert len(canonical) == 5 and len(set(canonical.values())) == 5
    for file in ('static/index.html', 'website/agamemnon-preview-agents.html', 'website/agamemnon-role-marks-preview.html', 'website/agamemnon-role-marks-preview.svg'):
        text = (ROOT / file).read_text()
        fragment = text.split(START, 1)[1].split(END, 1)[0]
        embedded = ET.fromstring(f'<root xmlns="http://www.w3.org/2000/svg">{fragment}</root>')
        symbols = embedded.findall(f'.//{NS}symbol')
        assert {s.get('id'): s.find(f'{NS}path').get('d') for s in symbols if s.get('id') in canonical} == canonical, file
        if file == 'static/index.html':
            legacy = {s.get('id') for s in symbols if s.get('id') in ('command', 'implement', 'scout', 'review')}
            assert legacy == {'command', 'implement', 'scout', 'review'}
        assert 'agamemnon-agent-marks.svg#soldier-' not in text, file
    dashboard = (ROOT / 'static/js/agentsDashboard.js').read_text()
    assert 'href="#soldier-${soldierVariant}"' in dashboard
    assert 'href="#${emblem}"' in dashboard  # Odysseus geometry retained, with reliable same-document delivery


def test_review_fixtures_embed_original_helmet_without_network_access():
    # A Windows checkout with core.autocrlf gives the SVG CRLF line endings;
    # the embedded copy keeps the LF bytes stored in git.
    helmet = (ROOT / 'static/branding/agamemnon-trojan-helmet.svg').read_bytes().replace(b'\r\n', b'\n')
    for page in ('agents', 'chat', 'workbench'):
        text = (ROOT / f'website/agamemnon-preview-{page}.html').read_text()
        src = re.search(r'<img class="brand-crest" src="data:image/svg\+xml;base64,([^"]+)" data-source="agamemnon-trojan-helmet.svg" alt=""', text)
        assert src and base64.b64decode(src.group(1)) == helmet
        if page != 'agents':
            assert '#soldier-' not in text
