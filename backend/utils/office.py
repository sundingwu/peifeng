"""Office 只读分发；格式实现位于对应技能。"""
from __future__ import annotations
from backend.utils.skill_libraries import format_reader
from pathlib import Path
from typing import Any, Optional
from backend.utils.text import file_sha256
from backend.utils.web import run_targets

def office_inspect_target(path: str, base_dir=None) -> str:
    """只读取一个 Office 文件的结构。"""
    target = Path(path)
    if not target.is_absolute():
        target = Path(base_dir or Path.cwd()) / target
    if not target.is_file():
        return _describe(target)
    digest = file_sha256(target)
    description = _describe(target)
    if file_sha256(target) != digest:
        raise RuntimeError('源文件在结构检查期间改变，请重新检查当前版本')
    return description + f'\n源文件 SHA-256：{digest}'

def _describe(target: Path) -> str:
    if not target.is_file():
        return f'【{target.name}】文件不存在'
    suffix = target.suffix.lower()
    try:
        if suffix == '.pptx':
            return format_reader('.pptx').inspect_document(target)
        if suffix == '.docx':
            return format_reader('.docx').inspect_document(target)
        if suffix == '.xlsx':
            return format_reader('.xlsx').inspect_document(target)
    except Exception as error:
        return f'【{target.name}】解析失败：{type(error).__name__}: {error}'
    return f'【{target.name}】不支持的类型（只支持 .pptx / .docx / .xlsx）'

def office_inspect(targets: list[dict], base_dir=None) -> str:
    """对象数组入口，参数逐项生效。"""
    return run_targets(lambda target: office_inspect_target(**target, base_dir=base_dir), targets, workers=4, base_dir=base_dir)
