"""本技能的只读格式解析，供通用 read 分发。"""
from pathlib import Path
from typing import Any, Optional
from backend.utils.text import image_source

def _docx(target: Path) -> str:
    from docx import Document
    document = Document(str(target))
    lines = [f'【{target.name}】{len(document.paragraphs)} 段，{len(document.tables)} 个表格']
    for number, section in enumerate(document.sections, 1):
        from docx.shared import Emu
        available = section.page_width - section.left_margin - section.right_margin
        lines.append(f'  分节 {number}：页面 {section.page_width.cm:.3f}×{section.page_height.cm:.3f} cm；正文可用宽 {Emu(available).cm:.3f} cm')
    for number, picture in enumerate(document.inline_shapes, 1):
        lines.append(f'  嵌图 {number}：{picture.width.cm:.3f}×{picture.height.cm:.3f} cm')
    lines.append('  检查范围：文档结构与尺寸；未验证Office原生分页视觉')
    for number, paragraph in enumerate(document.paragraphs, 1):
        text = paragraph.text.strip()
        style = paragraph.style.name if paragraph.style is not None else ''
        sizes = [run.font.size.pt for run in paragraph.runs if run.font.size]
        if sizes:
            low, high = (min(sizes), max(sizes))
            size = f' {low:g}pt' if low == high else f' {low:g}~{high:g}pt'
        else:
            size = ' 字号继承'
        shown = '（空段）' if not text else text if len(text) <= 60 else text[:60] + '…'
        lines.append(f'  {number:>3}. [{style}]{size} {shown}')
    for number, table in enumerate(document.tables, 1):
        header = ' | '.join((c.text.strip() for c in table.rows[0].cells)) if len(table.rows) else ''
        lines.append(f'  表格 {number}：{len(table.rows)}×{len(table.columns)}  首行：{header[:80]}')
    return '\n'.join(lines)

def docx_lines(target: Path) -> list[str]:
    from docx import Document
    document = Document(str(target))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(' | '.join((cell.text for cell in row.cells)))
    return lines

def inspect_document(target):
    return _docx(target)

def content_lines(target):
    return docx_lines(target)
