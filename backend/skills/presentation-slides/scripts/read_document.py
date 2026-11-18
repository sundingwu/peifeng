"""本技能的只读格式解析，供通用 read 分发。"""
from pathlib import Path
from typing import Any, Optional
from backend.utils.text import image_source

def _pptx(target: Path) -> str:
    from pptx import Presentation
    from pptx.util import Emu
    book = Presentation(str(target))
    lines = [f'【{target.name}】{len(book.slides)} 页，画布 {Emu(book.slide_width).cm:.1f}×{Emu(book.slide_height).cm:.1f} 厘米']
    hint = _master_size_hint(book)
    if hint:
        lines.append(f'  {hint}')
    for number, slide in enumerate(book.slides, 1):
        shapes = list(slide.shapes)
        lines.append(f'  第 {number} 页（{len(shapes)} 个对象）')
        for shape in shapes:
            lines.extend(_shape(shape, '    '))
    return '\n'.join(lines)

def _master_size_hint(book) -> str:
    """母版正文的默认字号：形状"字号继承"时，实际按它渲染。"""
    from pptx.oxml.ns import qn
    try:
        styles = book.slide_master._element.find(qn('p:txStyles'))
        body = styles.find(qn('p:bodyStyle')) if styles is not None else None
        level = body.find(qn('a:lvl1pPr')) if body is not None else None
        default = level.find(qn('a:defRPr')) if level is not None else None
        raw = default.get('sz') if default is not None else None
    except (AttributeError, TypeError):
        return ''
    if not raw:
        return ''
    return f'母版正文默认字号 {int(raw) / 100:g}pt（标「字号继承」的按这个渲染）'

def _shape(shape, indent: str='') -> list[str]:
    """一个形状的描述行；GROUP 递归展开（不展开就看不出组里的文字）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Emu
    try:
        box = f'{Emu(shape.left).cm:.1f},{Emu(shape.top).cm:.1f} {Emu(shape.width).cm:.1f}×{Emu(shape.height).cm:.1f}'
    except (TypeError, ValueError):
        box = '位置未知'
    kind = str(shape.shape_type or '对象').split(' (')[0]
    rows: list[str] = []
    if getattr(shape, 'has_table', False) and shape.has_table:
        table = shape.table
        rows.append(f'{indent}- {kind} [{box}] 表格 {len(table.rows)}×{len(table.columns)}')
    elif getattr(shape, 'has_text_frame', False) and shape.text_frame.text.strip():
        text = ' / '.join((p.strip() for p in shape.text_frame.text.splitlines() if p.strip()))
        if len(text) > 50:
            text = text[:50] + '…'
        rows.append(f'{indent}- {kind} [{box}]{_sizes(shape)}{_autofit(shape)} 「{text}」')
    else:
        rows.append(f'{indent}- {kind} [{box}]')
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            rows.extend(_shape(child, indent + '  '))
    return rows

def _sizes(shape) -> str:
    """字号：给区间而不是最大值；只报最大值时看不出"哪几处偏小"。

    段末标记（``<a:endParaRPr sz>``）与 run 字号不一致时会额外标出——行高按它算，
    只改 run 不改它，字变大反而会挤在一起，看起来像"改了没生效"。
    """
    from pptx.oxml.ns import qn
    sizes = [run.font.size.pt for paragraph in shape.text_frame.paragraphs for run in paragraph.runs if run.font.size]
    if not sizes:
        return ' 字号继承'
    low, high = (min(sizes), max(sizes))
    text = f' {low:g}pt' if low == high else f' {low:g}~{high:g}pt'
    ends: set[float] = set()
    for paragraph in shape.text_frame.paragraphs:
        end = paragraph._p.find(qn('a:endParaRPr'))
        if end is not None and end.get('sz'):
            ends.add(int(end.get('sz')) / 100)
    if ends and (min(ends) < low or max(ends) > high):
        text += f'（段末标记 {min(ends):g}pt，行高按它算）'
    return text

def _autofit(shape) -> str:
    """自动缩放：PowerPoint 会把文字压小以塞进框，实际显示字号可能远小于设定值。"""
    from pptx.oxml.ns import qn
    try:
        body = shape.text_frame._txBody.bodyPr
        fit = body.find(qn('a:normAutofit'))
    except (AttributeError, TypeError):
        return ''
    if fit is None:
        return ''
    raw = fit.get('fontScale')
    if not raw:
        return ' 自动缩放'
    return f' 自动缩放{int(raw) / 1000:g}%'

def pptx_lines(target: Path) -> list[str]:
    """按页列出全部文本：组合形状递归展开、表格逐行输出（版式模板的正文与表格常在这两类对象里）。"""
    from pptx import Presentation
    presentation = Presentation(str(target))
    lines: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        lines.append(f'[第 {index} 页]')
        for shape in pptx_all_shapes(slide.shapes):
            if getattr(shape, 'has_table', False):
                for row in shape.table.rows:
                    cells = [' '.join(cell.text.split()) for cell in row.cells]
                    if any(cells):
                        lines.append(' | '.join(cells))
                continue
            if not getattr(shape, 'has_text_frame', False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = ''.join((run.text for run in paragraph.runs)).strip()
                if text:
                    lines.append(text)
    return lines

def pptx_all_shapes(shapes):
    """递归展开组合形状，逐个产出（文本与表格都要，与 pptx 工具的遍历口径一致）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from pptx_all_shapes(shape.shapes)
        else:
            yield shape

def inspect_document(target):
    return _pptx(target)

def content_lines(target):
    return pptx_lines(target)
