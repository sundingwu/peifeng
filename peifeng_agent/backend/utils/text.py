"""按扩展名把文件解析成文本行——``read`` 工具的内部模块（浏览、检索、读全文都用它）。

文件名以下划线开头：**不会被当成工具加载**（见 ``shared/loader.parse_tool``）。
"""
from __future__ import annotations

from pathlib import Path

#: 需要专用库解析的文档后缀
DOC_SUFFIXES = (".docx", ".xlsx", ".pptx", ".pdf")

#: 可以按纯文本直接读的后缀（检索时据此决定要不要读内容）
TEXT_SUFFIXES = (
    ".md",
    ".txt",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".py",
    ".js",
    ".ts",
    ".sql",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".log",
)


#: 图片后缀：读不了像素内容，但给出格式、尺寸与字节数——核对出图是否成功靠它
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico")

#: 文本解码顺序：UTF-8（含 BOM）优先，其次中文 Windows 常见的 GBK/OEM 代码页，
#: 最后 latin-1 兜底（它永远不会失败，至少不会丢字节）
DECODE_CHAIN = ("utf-8-sig", "utf-8", "gbk", "cp936", "mbcs")


def decode_text(raw: bytes) -> tuple[str, str]:
    """按常见编码依次尝试，返回 ``(文本, 实际编码)``。

    **为什么不是纯 UTF-8**：Windows 上很多 .txt / .csv / .log 是记事本存的 ANSI（GBK），
    cmd 内置命令的输出也是 OEM 代码页——硬按 UTF-8 解会变成 ``����``（静默乱码，
    模型据此什么也读不到）。这里先严格试 UTF-8，失败才回退中文编码。
    """
    for encoding in DECODE_CHAIN:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1"), "latin-1"


def is_readable(path: Path) -> bool:
    """这个文件能否按文本（含文档解析）读取。"""
    return path.suffix.lower() in DOC_SUFFIXES or path.suffix.lower() in TEXT_SUFFIXES


def is_image(path: Path) -> bool:
    """这个文件是图片（内容读不了，但元信息可查）。"""
    return path.suffix.lower() in IMAGE_SUFFIXES


def image_info(path: Path) -> str:
    """图片元信息：格式、像素尺寸、色彩模式、字节数。

    出图之后核对"图有没有生成、尺寸对不对、是不是空图"靠这几项就够，
    不必再用脚本去解析像素。
    """
    size = path.stat().st_size
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            mode = image.mode
            fmt = image.format or path.suffix.lstrip(".").upper()
    except Exception:  # noqa: BLE001 - 缺 PIL 或文件损坏时退化到只报大小
        return f"{path.name}：图片文件，{size} 字节（无法解析像素尺寸）"
    return f"{path.name}：{fmt} 图片，{width}×{height} 像素，{mode} 模式，{size} 字节"


def content_lines(target: Path) -> list[str]:
    """把文件内容解析成文本行：.docx/.xlsx/.pptx/.pdf 由对应库解析，其余按文本解码。"""
    suffix = target.suffix.lower()
    if suffix == ".docx":
        return docx_lines(target)
    if suffix == ".xlsx":
        return xlsx_lines(target)
    if suffix == ".pptx":
        return pptx_lines(target)
    if suffix == ".pdf":
        return pdf_lines(target)
    raw = target.read_bytes()
    if b"\x00" in raw[:4096]:
        raise RuntimeError(f"这是二进制文件，无法按文本读取：{target.name}（{len(raw)} 字节）；请用相应的文档工具")
    text, _ = decode_text(raw)
    return text.splitlines()


def docx_lines(target: Path) -> list[str]:
    from docx import Document

    document = Document(str(target))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text for cell in row.cells))
    return lines


def xlsx_lines(target: Path) -> list[str]:
    from openpyxl import load_workbook

    workbook = load_workbook(str(target), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"[工作表 {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            if all(cell is None for cell in row):
                continue
            lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
    workbook.close()
    return lines


def pdf_lines(target: Path) -> list[str]:
    """按页取 PDF 的文字层；整份都没有文字层时说明是扫描件，别让人以为文件是空的。"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(target))
    except Exception as error:  # noqa: BLE001 - 损坏或加密的文件要给出原因
        raise RuntimeError(f"无法解析 PDF：{target.name}（{error}）") from None
    lines: list[str] = []
    empty_pages: list[int] = []
    for index, page in enumerate(reader.pages, start=1):
        lines.append(f"[第 {index} 页]")
        try:
            content = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页解析失败不影响其余页
            content = ""
        rows = [row.rstrip() for row in content.splitlines() if row.strip()]
        if rows:
            lines.extend(rows)
        else:
            empty_pages.append(index)
    if empty_pages and len(empty_pages) == len(reader.pages):
        return [
            f"PDF：{target.name}（共 {len(reader.pages)} 页，没有可提取的文字层）",
            "这份 PDF 可能是扫描件或纯图片页：文字在图片里，需先做 OCR 才能读。",
        ]
    if empty_pages:
        pages = "、".join(str(page) for page in empty_pages)
        lines.append(f"（第 {pages} 页没有文字层，可能是图片页）")
    return lines


def pptx_lines(target: Path) -> list[str]:
    """按页列出全部文本：组合形状递归展开、表格逐行输出（版式模板的正文与表格常在这两类对象里）。"""
    from pptx import Presentation

    presentation = Presentation(str(target))
    lines: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        lines.append(f"[第 {index} 页]")
        for shape in pptx_all_shapes(slide.shapes):
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [" ".join(cell.text.split()) for cell in row.cells]
                    if any(cells):
                        lines.append(" | ".join(cells))
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
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


def as_text_list(value, label: str = "参数", limit: int = 0) -> list[str]:
    """把模型给的批量参数规整成字符串列表。

    **为什么必须有**：模型常把数组参数传成单个字符串（``paths: "a.txt"``）。若直接
    ``for item in value`` 就会按**字符**遍历——读成 "a"、"."、"t" 这种路径，一次调用
    白花。这里统一收口：字符串包成单元素列表，非法类型明确报错，超量也报错。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value if isinstance(item, (str, int, float))]
    else:
        raise RuntimeError(f"{label} 需要字符串数组（收到 {type(value).__name__}）")
    items = [item.strip() for item in items if item.strip()]
    if limit and len(items) > limit:
        raise RuntimeError(f"{label} 一次最多 {limit} 个（当前 {len(items)} 个）：请分两次调用")
    return items


def as_dict_list(value, label: str = "参数", limit: int = 0) -> list[dict]:
    """把模型给的"对象数组"批量参数规整成字典列表。

    与 :func:`as_text_list` 同一套容错思路：单个对象自动包成单元素列表，
    非对象元素直接丢掉，超量报错（而不是静默截断）。
    """
    if value is None:
        return []
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [item for item in value if isinstance(item, dict)]
    else:
        raise RuntimeError(f"{label} 需要对象数组（收到 {type(value).__name__}）")
    if limit and len(items) > limit:
        raise RuntimeError(f"{label} 一次最多 {limit} 个（当前 {len(items)} 个）：请分两次调用")
    return items


def call_many(func, items: list[dict], label: str, base_dir=None, preview: str = "", **extra) -> str:
    """逐个调用同一个工具并分段汇总：一次调用完成多个目标。

    ``items`` 里每一项的键与单次调用的参数同名（多余的键忽略——模型偶尔会多写）。
    ``extra`` 是宿主注入的上下文（如 ``session_dir``），只在工具签名接受时才传。
    单个目标失败只记在它自己那一段，不影响其余项。
    """
    import inspect

    parameters = inspect.signature(func).parameters
    accepted = {
        name
        for name, parameter in parameters.items()
        if parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
        and name not in {"base_dir", *extra}
    }
    passed = {key: value for key, value in extra.items() if key in parameters}
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        cleaned = {key: value for key, value in item.items() if key in accepted}
        title = str(cleaned.get(preview) or f"第 {index} 项") if preview else f"第 {index} 项"
        try:
            body = func(**cleaned, base_dir=base_dir, **passed)
        except Exception as error:  # noqa: BLE001 - 单个失败不该拖垮整批
            body = f"失败：{error}"
        blocks.append(f"=== [{index}/{len(items)}] {title} ===\n{body}")
    return "\n\n".join(blocks)
