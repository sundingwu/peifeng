"""固定只读格式入口：实现归技能所有，不开放任意脚本或路径执行。"""
from functools import lru_cache
import importlib.util
from pathlib import Path

FORMATS = {'.docx': 'document-formatting', '.xlsx': 'spreadsheets',
           '.pptx': 'presentation-slides', '.pdf': 'pdf-documents'}


@lru_cache(maxsize=4)
def format_reader(suffix: str):
    skill = FORMATS[suffix]
    path = Path(__file__).resolve().parents[1] / 'skills' / skill / 'scripts/read_document.py'
    specification = importlib.util.spec_from_file_location('peifeng_read_' + skill.replace('-', '_'), path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
