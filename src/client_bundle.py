"""Canonical client bundles with legacy install-path copies for upgrades."""
from pathlib import Path
import json


def add_client_member(archive, path: Path, member: Path) -> None:
    """Ship both install paths without changing credentials or scoped endpoints.

    Existing automation can keep the legacy helper/skill path. New setup uses
    the canonical copy. Only package metadata and guidance are transformed;
    Python credential aliases and protocol identifiers are kept intact.
    """
    archive.write(path, member)
    canonical = Path(*(part.replace('odysseus', 'agamemnon') for part in member.parts))
    if canonical == member:
        return
    content = path.read_bytes()
    if path.suffix == '.md':
        text = content.decode('utf-8').replace('odysseus', 'agamemnon').replace('ODYSSEUS_', 'AGAMEMNON_')
        content = text.encode('utf-8')
    elif path.name == 'plugin.json':
        doc = json.loads(content)
        doc['name'] = 'agamemnon'
        content = json.dumps(doc, indent=2).encode('utf-8')
    archive.writestr(canonical.as_posix(), content)
