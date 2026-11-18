"""文本与文档：解码、docx/xlsx/pptx/pdf 转文本行、图片信息、SVG 结构、token 估算。"""
from __future__ import annotations
import hashlib
import io
from pathlib import Path
from typing import Any

def file_sha256(path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.ico')
DECODE_CHAIN = ('utf-8-sig', 'utf-8', 'gbk', 'cp936', 'mbcs')

def decode_text(raw: bytes) -> tuple[str, str]:
    """按常见编码依次尝试，返回 ``(文本, 实际编码)``。

    **为什么不是纯 UTF-8**：Windows 上很多 .txt / .csv / .log 是记事本存的 ANSI（GBK），
    cmd 内置命令的输出也是 OEM 代码页——硬按 UTF-8 解会变成 ``����``（静默乱码，
    模型据此什么也读不到）。这里先严格试 UTF-8，失败才回退中文编码。
    """
    for encoding in DECODE_CHAIN:
        try:
            return (raw.decode(encoding), encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return (raw.decode('latin-1'), 'latin-1')

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
            fmt = image.format or path.suffix.lstrip('.').upper()
    except Exception:
        return f'{path.name}：图片文件，{size} 字节（无法解析像素尺寸）'
    return f'{path.name}：{fmt} 图片，{width}×{height} 像素，{mode} 模式，{size} 字节'

def content_lines(target: Path) -> list[str]:
    """把文件内容解析成文本行：.docx/.xlsx/.pptx/.pdf 由对应库解析，其余按文本解码。"""
    suffix = target.suffix.lower()
    if suffix == '.docx':
        return docx_lines(target)
    if suffix == '.xlsx':
        return xlsx_lines(target)
    if suffix == '.pptx':
        return pptx_lines(target)
    if suffix == '.pdf':
        return pdf_lines(target)
    raw = target.read_bytes()
    if b'\x00' in raw[:4096]:
        raise RuntimeError(f'这是二进制文件，无法按文本读取：{target.name}（{len(raw)} 字节）；请用相应的文档工具')
    text, _ = decode_text(raw)
    return text.splitlines()

def docx_lines(target: Path) -> list[str]:
    from backend.utils.skill_libraries import format_reader
    return format_reader('.docx').content_lines(target)

def xlsx_lines(target: Path) -> list[str]:
    from backend.utils.skill_libraries import format_reader
    return format_reader('.xlsx').content_lines(target)

def pdf_lines(target: Path) -> list[str]:
    from backend.utils.skill_libraries import format_reader
    return format_reader('.pdf').content_lines(target)

def pptx_lines(target: Path) -> list[str]:
    from backend.utils.skill_libraries import format_reader
    return format_reader('.pptx').content_lines(target)

def pptx_all_shapes(shapes):
    from backend.utils.skill_libraries import format_reader
    return format_reader('.pptx').pptx_all_shapes(shapes)

def image_source(path: Path):
    """Office 和视觉模型共用图片入口，SVG 转为内存 PNG，不改源文件。"""
    if path.suffix.lower() != '.svg':
        return str(path)
    import io
    try:
        import resvg_py
        return io.BytesIO(resvg_py.svg_to_bytes(svg_path=str(path), width=1800, dpi=96))
    except (ImportError, OSError) as error:
        raise RuntimeError('SVG预览需要resvg-py Python库；不调用Inkscape。可交付原始SVG。') from error

def svg_structure(path: Path) -> str:
    """Describe actual SVG elements without rendering or evaluating resources."""
    from collections import Counter
    from defusedxml.ElementTree import parse
    root = parse(path).getroot()
    if root.tag.rsplit('}', 1)[-1] != 'svg':
        raise ValueError('文件根元素不是SVG')
    counts = Counter((node.tag.rsplit('}', 1)[-1] for node in root.iter() if isinstance(node.tag, str)))
    return '\n'.join([f'【{path.name}】SVG结构', f"width={root.get('width', '未声明')}；height={root.get('height', '未声明')}；viewBox={root.get('viewBox', '未声明')}", f"文本元素：{counts['text']}；tspan：{counts['tspan']}；路径：{counts['path']}；use引用：{counts['use']}；嵌图：{counts['image']}", '文本元素数只说明是否保留SVG文本节点；路径仍可编辑为几何对象。不据此判断视觉布局、文本溢出或数值正确性。'])
_CJK_RANGES = ((13312, 19903), (19968, 40959), (63744, 64255), (12288, 12351), (65280, 65519))

def _is_cjk(char: str) -> bool:
    """该字符是否按中文字符计价（更快：按码位比较，不用逐字符正则）。"""
    code = ord(char)
    return any((low <= code <= high for low, high in _CJK_RANGES))

def count_cjk(text: str) -> int:
    """统计文本里的中文字符数（含中文标点与全角字符）。"""
    return sum((1 for char in text if _is_cjk(char)))

def estimate_tokens(text: str) -> int:
    """按字符构成估算 token 数（无 ``usage`` 时的回退）。

    中文字符 0.6 token、其余字符 0.3 token；空串为 0。
    结果向下取整，至少为 1（非空文本不可能真的是 0 token）。
    """
    if not text:
        return 0
    cjk = count_cjk(text)
    return max(1, int(cjk * 0.6 + (len(text) - cjk) * 0.3))
