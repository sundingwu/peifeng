"""本技能的只读格式解析，供通用 read 分发。"""
from pathlib import Path
from typing import Any, Optional
from backend.utils.text import image_source


def record_tables(target: Path) -> list:
    from openpyxl import load_workbook
    workbook = load_workbook(target, read_only=True, data_only=True)
    try:
        return [(sheet.title, list(sheet.iter_rows(values_only=True))) for sheet in workbook.worksheets]
    finally:
        workbook.close()

def _xlsx(target: Path) -> str:
    from openpyxl import load_workbook
    book = load_workbook(str(target), read_only=True)
    lines = [f'【{target.name}】{len(book.sheetnames)} 个工作表']
    for name in book.sheetnames:
        sheet = book[name]
        rows = list(sheet.iter_rows(max_row=3, values_only=True))
        header = ' | '.join((str(cell) for cell in rows[0] or [] if cell is not None))
        lines.append(f'  「{name}」{sheet.max_row} 行 × {sheet.max_column} 列  表头：{header[:80]}')
    book.close()
    return '\n'.join(lines)

def xlsx_lines(target: Path) -> list[str]:
    from openpyxl import load_workbook
    workbook = load_workbook(str(target), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f'[工作表 {sheet.title}]')
        for row in sheet.iter_rows(values_only=True):
            if all((cell is None for cell in row)):
                continue
            lines.append(' | '.join(('' if cell is None else str(cell) for cell in row)))
    workbook.close()
    return lines

def inspect_document(target):
    return _xlsx(target)

def content_lines(target):
    return xlsx_lines(target)
