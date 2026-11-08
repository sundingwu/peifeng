"""只读的 Office 结构查看：给评审用（评审不能写文件，也不该写）。

把 .pptx / .docx / .xlsx 读成结构化描述（页/段落/工作表 + 文本 + 位置 + 字号），
让评审能核对"版面与字号真的改了吗"，而不是靠 `read` 把文件读成文本、
再去翻备份原文逐段对比（实测那样一次评审要 14 次 read）。
"""
from __future__ import annotations

from pathlib import Path

information = {
    "name": "office_inspect",
    "description": (
        "只读查看 .pptx / .docx / .xlsx 的**结构**：页数与每页对象（类型、位置、尺寸、字号、文本）、"
        "段落（序号、样式、字号）、工作表（行列数、表头）。"
        "核对版式/字号/页数这类改动时用它，比 read 读成纯文本更准，也不用去翻备份做对比"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要查看的文件（.pptx / .docx / .xlsx）"},
            "paths": {
                "type": "array",
                "description": "一次查看多个文件（改前改后对比就传两个）",
                "items": {"type": "string"},
            },
        },
        "required": [],
    },
}


def office_inspect(path: str = "", paths: list = None, base_dir=None) -> str:
    """把一个或多个 Office 文件读成结构描述。"""
    targets = [str(item) for item in (paths or []) if str(item).strip()]
    if str(path or "").strip():
        targets.insert(0, str(path))
    if not targets:
        raise RuntimeError("office_inspect 需要 path（单个）或 paths（多个）之一")

    root = Path(base_dir) if base_dir else Path.cwd()
    blocks = []
    for item in targets:
        target = Path(item)
        if not target.is_absolute():
            target = root / target
        blocks.append(_describe(target))
    return "\n\n".join(blocks)


def _describe(target: Path) -> str:
    if not target.is_file():
        return f"【{target.name}】文件不存在"
    suffix = target.suffix.lower()
    try:
        if suffix == ".pptx":
            return _pptx(target)
        if suffix == ".docx":
            return _docx(target)
        if suffix == ".xlsx":
            return _xlsx(target)
    except Exception as error:  # noqa: BLE001 - 解析失败要给出原因，不能让评审猜
        return f"【{target.name}】解析失败：{type(error).__name__}: {error}"
    return f"【{target.name}】不支持的类型（只支持 .pptx / .docx / .xlsx）"


def _pptx(target: Path) -> str:
    from pptx import Presentation
    from pptx.util import Emu

    book = Presentation(str(target))
    lines = [
        f"【{target.name}】{len(book.slides)} 页，"
        f"画布 {Emu(book.slide_width).cm:.1f}×{Emu(book.slide_height).cm:.1f} 厘米"
    ]
    hint = _master_size_hint(book)
    if hint:
        lines.append(f"  {hint}")
    for number, slide in enumerate(book.slides, 1):
        shapes = list(slide.shapes)
        lines.append(f"  第 {number} 页（{len(shapes)} 个对象）")
        for shape in shapes:
            lines.extend(_shape(shape, "    "))
    return "\n".join(lines)


def _master_size_hint(book) -> str:
    """母版正文的默认字号：形状"字号继承"时，实际按它渲染。"""
    from pptx.oxml.ns import qn

    try:
        styles = book.slide_master._element.find(qn("p:txStyles"))
        body = styles.find(qn("p:bodyStyle")) if styles is not None else None
        level = body.find(qn("a:lvl1pPr")) if body is not None else None
        default = level.find(qn("a:defRPr")) if level is not None else None
        raw = default.get("sz") if default is not None else None
    except (AttributeError, TypeError):
        return ""
    if not raw:
        return ""
    return f"母版正文默认字号 {int(raw) / 100:g}pt（标「字号继承」的按这个渲染）"


def _shape(shape, indent: str = "") -> list[str]:
    """一个形状的描述行；GROUP 递归展开（不展开就看不出组里的文字）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Emu

    try:
        box = (
            f"{Emu(shape.left).cm:.1f},{Emu(shape.top).cm:.1f} "
            f"{Emu(shape.width).cm:.1f}×{Emu(shape.height).cm:.1f}"
        )
    except (TypeError, ValueError):
        box = "位置未知"
    kind = str(shape.shape_type or "对象").split(" (")[0]
    rows: list[str] = []
    if getattr(shape, "has_table", False) and shape.has_table:
        table = shape.table
        rows.append(f"{indent}- {kind} [{box}] 表格 {len(table.rows)}×{len(table.columns)}")
    elif getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        text = " / ".join(p.strip() for p in shape.text_frame.text.splitlines() if p.strip())
        if len(text) > 50:
            text = text[:50] + "…"
        rows.append(f"{indent}- {kind} [{box}]{_sizes(shape)}{_autofit(shape)} 「{text}」")
    else:
        rows.append(f"{indent}- {kind} [{box}]")
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            rows.extend(_shape(child, indent + "  "))
    return rows


def _sizes(shape) -> str:
    """字号：给区间而不是最大值；只报最大值时看不出"哪几处偏小"。

    段末标记（``<a:endParaRPr sz>``）与 run 字号不一致时会额外标出——行高按它算，
    只改 run 不改它，字变大反而会挤在一起，看起来像"改了没生效"。
    """
    from pptx.oxml.ns import qn

    sizes = [
        run.font.size.pt
        for paragraph in shape.text_frame.paragraphs
        for run in paragraph.runs
        if run.font.size
    ]
    if not sizes:
        return " 字号继承"
    low, high = min(sizes), max(sizes)
    text = f" {low:g}pt" if low == high else f" {low:g}~{high:g}pt"
    ends: set[float] = set()
    for paragraph in shape.text_frame.paragraphs:
        end = paragraph._p.find(qn("a:endParaRPr"))
        if end is not None and end.get("sz"):
            ends.add(int(end.get("sz")) / 100)
    if ends and (min(ends) < low or max(ends) > high):
        text += f"（段末标记 {min(ends):g}pt，行高按它算）"
    return text


def _autofit(shape) -> str:
    """自动缩放：PowerPoint 会把文字压小以塞进框，实际显示字号可能远小于设定值。"""
    from pptx.oxml.ns import qn

    try:
        body = shape.text_frame._txBody.bodyPr
        fit = body.find(qn("a:normAutofit"))
    except (AttributeError, TypeError):
        return ""
    if fit is None:
        return ""
    raw = fit.get("fontScale")
    if not raw:
        return " 自动缩放"
    return f" 自动缩放{int(raw) / 1000:g}%"


def _docx(target: Path) -> str:
    from docx import Document

    document = Document(str(target))
    lines = [f"【{target.name}】{len(document.paragraphs)} 段，{len(document.tables)} 个表格"]
    for number, paragraph in enumerate(document.paragraphs, 1):
        text = paragraph.text.strip()
        style = paragraph.style.name if paragraph.style is not None else ""
        sizes = [run.font.size.pt for run in paragraph.runs if run.font.size]
        if sizes:
            low, high = min(sizes), max(sizes)
            size = f" {low:g}pt" if low == high else f" {low:g}~{high:g}pt"
        else:
            size = " 字号继承"
        shown = "（空段）" if not text else (text if len(text) <= 60 else text[:60] + "…")
        lines.append(f"  {number:>3}. [{style}]{size} {shown}")
    for number, table in enumerate(document.tables, 1):
        header = " | ".join(c.text.strip() for c in table.rows[0].cells) if len(table.rows) else ""
        lines.append(f"  表格 {number}：{len(table.rows)}×{len(table.columns)}  首行：{header[:80]}")
    return "\n".join(lines)


def _xlsx(target: Path) -> str:
    from openpyxl import load_workbook

    book = load_workbook(str(target), read_only=True)
    lines = [f"【{target.name}】{len(book.sheetnames)} 个工作表"]
    for name in book.sheetnames:
        sheet = book[name]
        rows = list(sheet.iter_rows(max_row=3, values_only=True))
        header = " | ".join(str(cell) for cell in (rows[0] or []) if cell is not None)
        lines.append(
            f"  「{name}」{sheet.max_row} 行 × {sheet.max_column} 列  表头：{header[:80]}"
        )
    book.close()
    return "\n".join(lines)
