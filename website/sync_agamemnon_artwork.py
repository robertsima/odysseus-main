"""Embed the licensed canonical Phalanx vectors for reliable local SVG use.

Run after changing static/branding/agamemnon-agent-marks.svg. Generated SVG
symbols contain the exact source geometry; never draw or maintain a second copy.
"""
from pathlib import Path
import base64
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'static/branding/agamemnon-agent-marks.svg'
SVG_NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', SVG_NS)
START = '<!-- AGAMEMNON SOLDIER SYMBOLS START (generated) -->'
END = '<!-- AGAMEMNON SOLDIER SYMBOLS END -->'


def update_symbols(text: str, symbols: str) -> str:
    block = START + '\n' + symbols + '\n' + END
    if START not in text:
        raise ValueError('Missing generated artwork insertion marker')
    return re.sub(re.escape(START) + r'.*?' + re.escape(END), lambda _: block, text, flags=re.S)


def sync() -> None:
    source = ET.parse(SOURCE).getroot()
    roles = ('primary', 'worker', 'scout', 'archer', 'reviewer', 'specialist', 'sword', 'helmet')
    soldier_symbols = [child for child in source if child.tag == f'{{{SVG_NS}}}symbol' and child.get('id') in {f'soldier-{role}' for role in roles}]
    legacy_symbols = [child for child in source if child.tag == f'{{{SVG_NS}}}symbol' and child.get('id') in ('command', 'implement', 'scout', 'review')]
    assert len(soldier_symbols) == len(roles) and len(legacy_symbols) == 4
    def serialize(items):
        return '\n'.join(line for symbol in items for line in ET.tostring(symbol, encoding='unicode').strip().splitlines() if line.strip())
    symbols = serialize(soldier_symbols)
    production_symbols = serialize(soldier_symbols + legacy_symbols)
    # HTML uses same-document fragments, avoiding file:// and cross-origin SVG
    # <use> restrictions. Hidden definitions do not affect either theme layout.
    for relative in ('static/index.html', 'website/agamemnon-preview-agents.html', 'website/agamemnon-role-marks-preview.html', 'website/agamemnon-appearance-preview.html'):
        target = ROOT / relative
        embedded = production_symbols if relative == 'static/index.html' else symbols
        text = update_symbols(target.read_text(), f'<svg xmlns="{SVG_NS}" style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true" focusable="false"><defs>\n{embedded}\n</defs></svg>')
        text = re.sub(r'(?:\.\./static/branding/|https?://[^"\s]+/static/branding/)agamemnon-agent-marks\.svg#soldier-', '#soldier-', text)
        text = text.replace('${agentMarksUrl()}#soldier-', '#soldier-') if relative == 'static/index.html' else text
        target.write_text(text)
    target = ROOT / 'website/agamemnon-role-marks-preview.svg'
    text = update_symbols(target.read_text(), f'<defs>\n{symbols}\n</defs>')
    text = text.replace('../static/branding/agamemnon-agent-marks.svg#soldier-', '#soldier-')
    target.write_text(text)
    helmet = base64.b64encode((ROOT / 'static/branding/agamemnon-trojan-helmet.svg').read_bytes()).decode('ascii')
    for page in ('agents', 'chat', 'workbench'):
        target = ROOT / f'website/agamemnon-preview-{page}.html'
        text = target.read_text()
        # Data URI is only for portable review fixtures; production serves the
        # original SVG asset with normal same-origin caching and attribution.
        image = f'<img class="brand-crest" src="data:image/svg+xml;base64,{helmet}" data-source="agamemnon-trojan-helmet.svg" alt=""'
        text, count = re.subn(r'<img class="brand-crest" src="[^"]*"(?: data-source="[^"]*")? alt=""', lambda _: image, text)
        assert count == 1, target
        target.write_text(text)


if __name__ == '__main__':
    sync()
