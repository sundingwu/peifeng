"""read 工具（看）：读文件内容、列目录。

目标类型由路径自己决定，不用另选参数：

- ``path`` 指向文件（可传一组路径批量）：内容按行输出（带行号，可 offset/limit 分段）；
  ``.docx``/``.xlsx``/``.pptx`` 由文档库解析为文本；给 ``grep`` 就只回命中的行
- ``path`` 是目录：列出条目（名称、类型、大小、修改时间）；给后缀（``.md``）只列这类文件，
  给关键词/正则就在其中搜内容，``recursive=True`` 一直往下翻

**不设人为上限**：命中多少给多少、目录多深翻多深、批量多少读多少——
输出过大由工具层的统一兜底处理（超限落盘并把路径给模型），不在这里切。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。

**目录即能力**：本文件是**完整实现**，不转发到别处——它就是这个角色真正在跑的读逻辑。
worker 共用 ``executor/common/tool/read.py``；planner 与 critic 各自一份：它们没有写权限，
读的方式日后也可能与 worker 不同，复制而非共享是为了能各改各的（改实现要三份同步）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from peifeng_agent.backend.utils.concurrency import run_parallel_safe
from peifeng_agent.backend.utils.text import as_text_list, content_lines, image_info, is_image, is_readable

#: 批量读的并发度：本地读盘，8 路足够快也不会把磁盘打满
READ_WORKERS = 8

information = {
    "name": "read",
    "description": "负责读取文件内容和探查目录下存在哪些文件",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": ["string", "array"], "items": {"type": "string"},
                     "description": "要看的路径：一个字符串，或一组路径的数组。"
                                    "文件→读内容；目录→列出其中的条目。传数组时结果按 ===== 路径 ===== 分段"},
            "grep": {"type": "string",
                     "description": "从**本次输出**里筛出含它的行（不会去读你未指定的文件）。"
                                    "默认按字面量匹配，`C++`、`file(name)`、`/usr/bin/` 都照原样找"},
            "regex": {"type": "boolean",
                      "description": "grep 是否按正则匹配（默认 false 按字面量）"},
            "offset": {"type": "integer", "description": "从第几行开始（默认 1）"},
            "limit": {"type": "integer", "description": "最多返回多少行（不传就全部）"},
            "recursive": {"type": "boolean",
                          "description": "目标是目录时是否连子目录一起列（默认 false 只看当前层）"},
        },
        "required": [],
    },
}


def filter_lines(body: str, pattern: str, regex: bool,
                 offset: Optional[int], limit: Optional[int], label: str) -> str:
    """从 body 里筛出含 pattern 的行；行号保留原文位置，再按 offset/limit 分页。

    ``grep`` 只作用于**已经拿到的文本**——不会因为给了关键词就去读别的文件。
    """
    matcher = compile(pattern, regex)
    lines = body.splitlines()
    found = [(index, line) for index, line in enumerate(lines, 1) if matcher.search(line)]
    if not found:
        return f"{label} 未找到 {pattern!r}（共 {len(lines)} 行）"
    selected, more = page(found, offset, limit)
    head = f"{label} 命中 {len(found)} 处（共 {len(lines)} 行）"
    rows = [f"{number}:{line}" for number, line in selected]
    if more:
        rows.append(more)
    return "\n".join([head, *rows])


def page(items: list, offset: Optional[int], limit: Optional[int]) -> tuple[list, str]:
    """按 offset/limit 切片，返回 ``(选中项, 续读提示或空串)``。"""
    start = as_int(offset, "offset", 1)
    if start < 1:
        raise RuntimeError(f"offset 需要是正整数（当前：{offset!r}）")
    begin = start - 1
    count = as_int(limit, "limit", 0)
    if limit is not None and count <= 0:
        raise RuntimeError(f"limit 需要是正整数（当前：{limit!r}）")
    if not items:
        return [], ""
    if begin >= len(items):
        raise RuntimeError(f"起始行号超出范围：offset={start}，共 {len(items)} 行")
    end = begin + count if count else len(items)
    selected = items[begin:end]
    if end < len(items):
        return selected, f"（已截断：返回第 {begin + 1}-{end} 行，共 {len(items)} 行；继续读取请用 offset={end + 1}）"
    return selected, ""


def read(
    path: Any = None,
    grep: str = "",
    regex: bool = False,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    recursive: bool = False,
    base_dir=None,
) -> str:
    """看文件内容，或探查目录里有什么。

    ``grep`` 只从**本次输出**里筛行，不会去读你没指定的文件——想搜一批文件的内容，
    把它们的路径放进 ``path`` 数组一次读，再让 grep 筛。
    """
    targets = as_text_list(path, "path", 0)
    if not targets:
        raise RuntimeError("read 需要 path：一个路径，或一组路径的数组")
    if len(targets) == 1:
        return read_one(
            targets[0], grep=grep, regex=regex, offset=offset, limit=limit,
            recursive=recursive, base_dir=base_dir,
        )
    return read_batch(
        targets, grep=grep, regex=regex, offset=offset, limit=limit,
        recursive=recursive, base_dir=base_dir,
    )


def read_one(
    target_text: str,
    *,
    grep: str = "",
    regex: bool = False,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    recursive: bool = False,
    base_dir=None,
) -> str:
    """拿这个目标的文本：目录→条目清单，文件→内容；再筛行、再分页。"""
    target = resolve(target_text, base_dir)
    if target.is_dir():
        body = list_entries(target, recursive)
    elif target.is_file():
        if is_image(target):
            return image_info(target)
        body = "\n".join(content_lines(target))
    else:
        raise RuntimeError(f"路径不存在：{target_text}")

    if grep:
        return filter_lines(body, grep, regex, offset, limit, target_text)

    lines = body.splitlines()
    selected, more = page(list(enumerate(lines, 1)), offset, limit)
    rows = [f"{number}:{line}" for number, line in selected]
    if more:
        rows.append(more)
    return "\n".join(rows) if rows else "（空）"


def read_batch(
    items: List[str],
    *,
    grep: str = "",
    regex: bool = False,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    recursive: bool = False,
    base_dir=None,
) -> str:
    """一次看多个目标：并发跑，每段前带 ``===== 路径 =====``（条数与总字数都不设限）。

    文件和目录可以混放——各自按类型处理。grep 对每一段各筛一次。
    """
    results = run_parallel_safe(
        lambda item: read_one(
            item, grep=grep, regex=regex, offset=offset, limit=limit,
            recursive=recursive, base_dir=base_dir,
        ),
        items,
        workers=READ_WORKERS,
        thread_prefix="read",
    )
    blocks: List[str] = []
    for item, (body, error) in zip(items, results):
        blocks.append(f"===== {item} =====\n{body if error is None else f'错误：{error}'}")
    return "\n\n".join(blocks)


def compile(keyword: str, regex: bool):
    """把关键词编译成匹配器：默认按子串、忽略大小写；regex=True 时按正则。"""
    if regex:
        try:
            return re.compile(keyword, re.IGNORECASE)
        except re.error as error:
            raise RuntimeError(
                f"正则无法编译：{keyword}（{error}）。改成合法正则（如 C\\+\\+），"
                "或把 regex 设为 false 按字面量搜"
            ) from None
    return re.compile(re.escape(keyword), re.IGNORECASE)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def list_entries(directory: Path, recursive: bool = False) -> str:
    """列出目录里的条目；``recursive=True`` 时连子目录一起展开（**只列名字，不读文件内容**）。

    头部给出汇总（文件数、目录数、合计字节数）：清点目录不必再去跑 shell 统计。
    """
    rows: list[str] = []
    files = folders = size = 0
    for level_lines, counts in walk(directory, recursive, prefix=""):
        rows.extend(level_lines)
        files += counts[0]
        folders += counts[1]
        size += counts[2]
    if not files and not folders:
        return f"目录：{directory}（无匹配条目）"
    head = f"目录：{directory}（文件 {files} 个、目录 {folders} 个，合计 {size} 字节）"
    return "\n".join([head, *rows])


def walk(directory: Path, recursive: bool, prefix: str):
    """按层产出 ``(本层条目行, (文件数, 目录数, 合计字节))``；``recursive`` 为真就一路下钻。"""
    try:
        entries = sorted(directory.iterdir())
    except OSError as error:
        yield [f"{prefix}（无法读取：{error}）"], (0, 0, 0)
        return
    lines: list[str] = []
    children: list[Path] = []
    files = size = 0
    for entry in entries:
        if entry.is_dir():
            lines.append(f"{prefix}- {entry.name}/  目录")
            children.append(entry)
            continue
        stat = entry.stat()
        stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{prefix}- {entry.name}  文件  {stat.st_size} 字节  {stamp}")
        files += 1
        size += stat.st_size
    yield lines, (files, len(children), size)
    if not recursive:
        return
    for child in children:
        yield from walk(child, True, prefix=f"  {prefix}")


def as_int(value, name: str, fallback: int) -> int:
    """把模型给的参数转成整数；缺省用 fallback，非法值给中文提示。"""
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} 需要是整数（当前：{value!r}）") from None


