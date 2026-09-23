"""Read one operator-selected local skill, without following links or executing it."""
import os
from pathlib import Path

from .skill_importer import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, SkillImportError, _is_text_file


def read_local_skill_bundle(directory):
    source = Path(directory).expanduser()
    if source.is_symlink() or (hasattr(source, 'is_junction') and source.is_junction()):
        raise SkillImportError('Select a real skill directory, not a symlink/junction')
    root = source.resolve(strict=True)
    if not root.is_dir() or not (root / 'SKILL.md').is_file():
        raise SkillImportError('Select one skill directory containing SKILL.md')
    files = {}
    total = 0
    # os.walk works on native Python 3.11 too. Reject links rather than silently
    # importing an incomplete bundle. No script or binary is ever run.
    def fail_walk(error):
        raise error

    for current_path, directories, names in os.walk(root, followlinks=False, onerror=fail_walk):
        current = Path(current_path)
        for name in directories + names:
            path = current / name
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise SkillImportError(f'Symlinks/junctions are not supported: {path.relative_to(root)}')
            if not path.resolve().is_relative_to(root):
                raise SkillImportError('Resource escapes selected skill directory')
        if len(current.relative_to(root).parts) > 4:
            raise SkillImportError('Skill bundle exceeds directory depth limit')
        for name in names:
            path = current / name
            if not _is_text_file(name):
                raise SkillImportError(f'Unsupported non-text resource: {path.relative_to(root)}')
            if len(files) >= MAX_FILES or path.stat().st_size > MAX_FILE_BYTES:
                raise SkillImportError('Skill bundle exceeds file count or file size limit')
            content = path.read_text(encoding='utf-8')
            total += len(content.encode('utf-8'))
            if total > MAX_TOTAL_BYTES:
                raise SkillImportError('Skill bundle exceeds total size limit')
            files[path.relative_to(root).as_posix()] = content
    return files
