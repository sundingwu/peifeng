"""本技能的只读格式解析，供通用 read 分发。"""
from pathlib import Path
from typing import Any, Optional
from backend.utils.text import image_source


def page_count(target: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(target).pages)


def selected_pages(target: Path, pages: list[int]) -> str:
    from pypdf import PdfReader
    reader = PdfReader(target)
    if not isinstance(pages, list) or not pages or any(type(page) is not int or not 1 <= page <= len(reader.pages) for page in pages):
        raise ValueError('pages必须为有效的1起始PDF页码数组')
    parts = []
    for number in pages:
        text = reader.pages[number-1].extract_text() or ''
        parts.append(f'[page {number}]\n' + (text if text.strip() else '无可提取文字层，不能视为已读；需要本地OCR或用户提供文字。'))
    return '\n'.join(parts)

def pdf_lines(target: Path) -> list[str]:
    """按页取 PDF 的文字层；整份都没有文字层时说明是扫描件，别让人以为文件是空的。"""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(target))
    except Exception as error:
        raise RuntimeError(f'无法解析 PDF：{target.name}（{error}）') from None
    lines: list[str] = []
    empty_pages: list[int] = []
    for index, page in enumerate(reader.pages, start=1):
        lines.append(f'[第 {index} 页]')
        try:
            content = page.extract_text() or ''
        except Exception:
            content = ''
        rows = [row.rstrip() for row in content.splitlines() if row.strip()]
        if rows:
            lines.extend(rows)
        else:
            empty_pages.append(index)
    if empty_pages and len(empty_pages) == len(reader.pages):
        return [f'PDF：{target.name}（共 {len(reader.pages)} 页，没有可提取的文字层）', '这份 PDF 可能是扫描件或纯图片页：文字在图片里，需先做 OCR 才能读。']
    if empty_pages:
        pages = '、'.join((str(page) for page in empty_pages))
        lines.append(f'（第 {pages} 页没有文字层，可能是图片页）')
    return lines

def content_lines(target):
    return pdf_lines(target)
