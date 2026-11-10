"""render_pages 工具：把 .pptx / .pdf 逐页渲染成 PNG，供 image_describe 核对版面。

产出文件是给人看的图，不是交付物：默认落在会话中间目录，交付时不留。
渲染能力取决于本机环境——PDF 靠 poppler 的 pdftoppm，PPT 靠 PowerPoint；
缺哪个就在返回里说明，别让人以为文件坏了。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

from peifeng_agent.backend.utils.text import as_dict_list, call_many

import shutil
import subprocess
from pathlib import Path

#: 渲染宽度（像素）；16:9 页面按 1400 宽已经能看清正文小字
DEFAULT_WIDTH = 1400
#: PDF 渲染的分辨率（dpi），150 与上面 1400 宽大致相当
PDF_DPI = 150
#: PPT 渲染用的字面量脚本（走 PowerPoint COM，Windows 与 Office 都在时才可用）
POWERPOINT_SCRIPT = """
$ErrorActionPreference = "Stop"
$ppt = New-Object -ComObject PowerPoint.Application
try {{
    $pres = $ppt.Presentations.Open("{source}", $true, $false, $false)
    try {{ $pres.Export("{target}", "PNG", {width}, 0) }}
    finally {{ $pres.Close() }}
}} finally {{ $ppt.Quit() }}
"""

information = {
    "name": "render_pages",
    "description": (
        "把 .pptx 或 .pdf 逐页渲染成 PNG，返回图片路径清单。"
        "用于检查排版：文字有没有溢出、图形有没有压到文字、页面是否空洞。"
        "渲染图是过程产物，交付时清理掉。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要渲染的 .pptx 或 .pdf（相对 <项目目录> 或绝对路径）"},
            "width": {"type": "integer", "description": "渲染宽度像素（默认 1400，16:9 页面够看清小字）"},
            "pages": {"type": "array", "description": "只渲染这些页（页码 1 起）；省略则全部渲染"},
        },
        "required": [],
    },
}


information["parameters"]["properties"]["paths"] = {
    "type": "array",
    "description": (
        "一次处理多个目标（与 path 互斥，）：每项是对象，"
        "键与单次调用的参数同名（如 {path: ...}）。要产出多个时用这个，不要一个一次调用"
    ),
    "items": {
        "type": "object",
        # 元素字段 = 单次调用的全部参数（只留类型，不复制描述，控制体积）；
        # 只声明文件名会让模型以为不能传 blocks/slides，生成出空文档
        "properties": {
            key: {"type": spec.get("type", "string")}
            for key, spec in information["parameters"]["properties"].items()
            if key != "paths"
        },
        "required": ["path"],
    },
}


def render_pages(
    path: str = "",
    width: int = DEFAULT_WIDTH,
    pages: list = None,
    paths: list = None,
    base_dir=None,
    session_dir=None,
) -> str:
    """渲染并返回图片路径清单；输出放会话中间目录，相对路径以 base_dir 为基准。"""
    batch = as_dict_list(paths, "paths", MAX_BATCH)
    if batch:
        return call_many(render_pages, batch, "paths", base_dir=base_dir, preview="path", session_dir=session_dir)
    if not str(path or "").strip():
        raise RuntimeError("render_pages 需要 path（单个）或 paths（多个）之一")
    source = resolve(path, base_dir)
    if not source.is_file():
        raise RuntimeError(f"文件不存在：{path}")
    suffix = source.suffix.lower()
    if suffix not in (".pptx", ".ppt", ".pdf"):
        raise RuntimeError(f"只支持 .pptx 与 .pdf（当前：{suffix or '无后缀'}）")

    output = (resolve(session_dir, None) if session_dir else source.parent) / "render"
    if output.exists():
        shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    size = max(600, min(int(width or DEFAULT_WIDTH), 2400))
    wanted = [int(page) for page in (pages or []) if str(page).strip()]

    if suffix == ".pdf":
        images = render_pdf(source, output, size, wanted)
    else:
        images = render_ppt(source, output, size)
        if wanted:
            images = [item for item in images if page_no(item) in wanted]
    if not images:
        raise RuntimeError(f"没有渲染出任何页：{source.name}（检查页码范围或文件是否损坏）")

    lines = [f"已渲染 {len(images)} 页（{source.name}，宽 {size} 像素）："]
    lines.extend(f"  {item}" for item in images)
    lines.append("下一步用 image_describe(path=<上面某张图>, question=<要核对什么>) 逐页看版面")
    return "\n".join(lines)


def render_pdf(source: Path, output: Path, width: int, wanted: list) -> list[str]:
    """PDF 走 poppler 的 pdftoppm（MiKTeX 一般自带）。"""
    tool = shutil.which("pdftoppm")
    if not tool:
        raise RuntimeError("没有找到 pdftoppm：渲染 PDF 需要 poppler（装 MiKTeX 或 poppler 后重试）")
    command = [tool, "-png", "-r", str(PDF_DPI)]
    if wanted:
        command += ["-f", str(min(wanted)), "-l", str(max(wanted))]
    command += [str(source), str(output / "page")]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"pdftoppm 失败：{(result.stderr or result.stdout).strip()[:300]}")
    return [str(path) for path in sorted(output.glob("page*.png"))]


def render_ppt(source: Path, output: Path, width: int) -> list[str]:
    """PPT 走 PowerPoint COM（本机装了 Office 才有）。"""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("没有找到 PowerShell：渲染 PPT 需要本机有 Office 与 PowerShell")
    script = POWERPOINT_SCRIPT.format(source=str(source), target=str(output), width=width)
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise RuntimeError(f"PowerPoint 渲染失败（需要本机装 Office）：{detail}")
    return [str(path) for path in sorted(output.glob("*.png"), key=page_no)]


def page_no(path) -> int:
    """从文件名里取页码（幻灯片3.png / page-03.png 都能认）。"""
    digits = "".join(char for char in Path(path).stem if char.isdigit())
    return int(digits) if digits else 0


def resolve(path, base_dir) -> Path:
    """相对路径以 base_dir 为基准；绝对路径原样使用。"""
    target = Path(str(path))
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target
