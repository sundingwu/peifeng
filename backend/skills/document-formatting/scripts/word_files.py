"""本技能的格式校验与原子保存。"""
import zipfile
from pathlib import Path
from backend.utils.skill_runtime import atomic_output, _UNSET

def validate_office(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() or len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('Office包校验失败')
    if path.suffix.lower() in {'.docx', '.dotx'}:
        from docx import Document
        Document(path)

def save_office(document, target: Path, expected=_UNSET, *, sources=()) -> None:
    with atomic_output(target, expected=expected, sources=sources) as candidate:
        document.save(candidate)
        validate_office(candidate)
