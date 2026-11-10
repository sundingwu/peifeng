"""read 工具（看）：读文件内容、列目录。

两套参数，看名字就知道走哪条路：

- ``file``（或 ``paths`` 批量）：内容按行输出（带行号，可 offset/limit 分段）；
  ``.docx``/``.xlsx``/``.pptx`` 由文档库解析为文本；给 ``grep`` 就只回命中的行
- ``dir``：列出条目（名称、类型、大小、修改时间）；给后缀（``.md``）只列这类文件，
  给关键词/正则就在文件内容里搜，``recursive=True`` 一直往下翻

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
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, List, Optional

from peifeng_agent.backend.utils.concurrency import run_parallel_safe
from peifeng_agent.backend.utils.text import as_text_list, content_lines, image_info, is_image, is_readable

#: 批量读的并发度：本地读盘，8 路足够快也不会把磁盘打满
READ_WORKERS = 8

information = {
    "name": "read",
    "description": (
        "查看：读文件内容、读一批文件、列目录条目、在其中检索关键词或正则。\n"
        "docx / xlsx / pptx 自动解析为文本，图片返回尺寸与格式等基本信息。\n"
        "内容不截断：很大时完整写入临时文件，回执给出路径供分段查看。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # ---- 二选一：读文件 / 列目录 ----
            "file": {"type": "string", "description": "【读文件】要读的文件路径（与 dir 二选一）"},
            "dir": {"type": "string", "description": "【列目录】要列的目录路径（与 file 二选一）"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "【读文件】一次读多个文件（与 file 二选一），结果按 ===== 路径 ===== 分段"},
            # ---- 筛选 ----
            "grep": {"type": "string",
                     "description": "【通用】筛选。读文件时是内容关键词或正则；列目录时是后缀（.md）或关键词/正则。"
                                    "自动识别：.md 这类当后缀、/…/ 或含 [ ] ( ) | ^ $ * + ? \\ 当正则、其余当关键词"},
            # ---- 仅读文件 ----
            "offset": {"type": "integer", "description": "【读文件】起始行号（1 起；不传从第 1 行开始）"},
            "limit": {"type": "integer", "description": "【读文件】最多返回行数（不传就返回全部）"},
            # ---- 仅列目录 ----
            "recursive": {"type": "boolean", "description": "【列目录】是否递归到底（true = 不限层数）"},
        },
        "required": [],
    },
}


def classify_grep(value: str) -> tuple[str, str]:
    """把 ``grep`` 认成三种之一：``suffix``（后缀）/ ``regex``（正则）/ ``keyword``（关键词）。

    用户一次只想表达"筛什么"，不该为"这是不是正则"再学一个参数：
    - ``.md``、``*.txt`` 这种 → 按文件名后缀筛
    - 用 ``/`` 包起来，或含正则元字符 → 按正则匹配内容
    - 其余 → 当普通关键词
    """
    text = (value or "").strip()
    if not text:
        return "none", ""
    plain = text.lstrip("*")
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", plain):
        return "suffix", plain.lower()
    if len(text) > 2 and text.startswith("/") and text.endswith("/"):
        return "regex", text[1:-1]
    if any(ch in text for ch in ("\\", "[", "]", "(", ")", "|", "^", "$", "+", "?")):
        return "regex", text
    return "keyword", text


def read(
    file: str = "",
    dir: str = "",
    paths: Optional[List[str]] = None,
    grep: str = "",
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    recursive: bool = False,
    base_dir=None,
) -> str:
    """读文件或列目录——两套参数，看参数名就知道走哪条路。

    **读文件**（``file``，或 ``paths`` 一次读多个）
    - 不给 ``grep`` → 全文，可用 ``offset`` / ``limit`` 分段
    - 给关键词或正则 → 只回命中的行

    **列目录**（``dir``）
    - 不给 ``grep`` → 列出条目
    - 给后缀（``.md``）→ 只列这类文件
    - 给关键词或正则 → 在文件内容里搜
    - ``recursive=true`` → 一直往下翻，不设层数限制
    """
    batch = as_text_list(paths, "paths", 0)
    if batch:
        return read_batch(batch, offset=offset, limit=limit, grep=grep, base_dir=base_dir)

    target_text = str(file or dir or "").strip()
    if not target_text:
        raise RuntimeError("read 需要 file（读文件）、dir（列目录）或 paths（多个文件）之一")
    target = resolve(target_text, base_dir)
    kind, cleaned = classify_grep(grep)

    # ---- 列目录 ----
    if dir:
        if not target.is_dir():
            raise RuntimeError(f"目录不存在：{dir}")
        if kind == "suffix":
            return list_entries(target, f"*{cleaned}", recursive)
        if kind in ("keyword", "regex"):
            return grep_lines(target, cleaned, kind == "regex", recursive, dir)
        return list_entries(target, "*", recursive)

    # ---- 读文件 ----
    if not target.is_file():
        raise RuntimeError(f"文件不存在：{file}")
    if is_image(target):
        return image_info(target)
    if kind in ("keyword", "regex"):
        return grep_lines(target, cleaned, kind == "regex", False, file)
    return format_lines(str(target), content_lines(target), offset, limit)


def read_batch(
    items: List[str],
    *,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    grep: str = "",
    base_dir=None,
) -> str:
    """一次读多个文件：并发读，每段前带 ``===== 路径 =====``（条数与总字数都不设限）。"""
    results = run_parallel_safe(
        lambda item: read(file=item, offset=offset, limit=limit, grep=grep, base_dir=base_dir),
        items,
        workers=READ_WORKERS,
        thread_prefix="read",
    )
    blocks: List[str] = []
    for item, (body, error) in zip(items, results):
        blocks.append(f"===== {item} =====\n{body if error is None else f'错误：{error}'}")
    return "\n\n".join(blocks)

def grep_lines(target: Path, keyword: str, regex: bool, recursive: bool, shown: str) -> str:
    """按行检索：文件内检索，或目录下检索（``recursive`` 时一直往下翻，不设层数）。"""
    matcher = compile(keyword, regex)
    if target.is_file():
        lines = content_lines(target)
        hits, matched = match_lines(lines, matcher, 0)
        if not hits:
            return f"{target} 中未找到 {keyword}（共 {len(lines)} 行）"
        return "\n".join([f"{target} 命中 {matched} 处（共 {len(lines)} 行）", *hits])

    if not target.is_dir():
        raise RuntimeError(f"路径不存在：{shown}")
    hits, matched, scanned = scan(target, matcher, recursive)
    if not hits:
        return f"{target} 下未找到 {keyword}（扫描 {scanned} 个文件）"
    return "\n".join([f"{target} 下命中 {matched} 处（扫描 {scanned} 个文件）", *hits])


def scan(root: Path, matcher, recursive: bool) -> tuple[list[str], int, int]:
    """遍历目录收集命中，返回 ``(输出行, 命中数, 扫描文件数)``。

    命中数单独统计（输出行里可能混着"文件名匹配"这类行）。
    不设命中上限、不设文件数上限：要全部就给全部，输出过大由工具层统一兜底（落盘给路径）。
    """
    hits: list[str] = []
    matched = 0
    scanned = 0
    queue: list[Path] = [root]
    while queue:
        directory = queue.pop(0)
        try:
            entries = sorted(Path(directory).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if recursive:
                    queue.append(entry)
                continue
            try:
                shown = f"{entry.relative_to(root).as_posix()}"
            except ValueError:
                shown = entry.name
            # 文件名本身命中就算一条：read(dir="detail", grep="task1_step") 要能直接列出文件，
            # 而不是去这些文件的内容里找这个字符串
            if matcher.search(entry.name):
                hits.append(f"{shown}  （文件名匹配）")
                matched += 1
            if not is_readable(entry):
                continue
            scanned += 1
            try:
                lines = content_lines(entry)
            except Exception:  # noqa: BLE001 - 单个文件读不了就跳过
                continue
            rows, count = match_lines(lines, matcher, 0, prefix=f"{shown}:")
            hits.extend(rows)
            matched += count
    return hits, matched, scanned

def compile(keyword: str, regex: bool):
    """把关键词编译成匹配器：默认按子串、忽略大小写；regex=True 时按正则。"""
    if regex:
        try:
            return re.compile(keyword, re.IGNORECASE)
        except re.error as error:
            raise RuntimeError(f"正则表达式无效：{keyword}（{error}）") from None
    return re.compile(re.escape(keyword), re.IGNORECASE)


def match_lines(lines: list[str], matcher, context: int, prefix: str = "") -> tuple[list[str], int]:
    """返回 ``(输出行, 命中数)``：命中行用 ``行号:``，上下文行用 ``行号-``。

    统计只算命中行，上下文行不计入——否则命中 1 处会变成命中 3 处。
    """
    found: list[int] = [index for index, line in enumerate(lines) if matcher.search(line)]
    if not found:
        return [], 0
    rows: list[str] = []
    shown_until = -1
    for index in found:
        first = max(0, index - context)
        last = min(len(lines) - 1, index + context)
        for current in range(first, last + 1):
            if current <= shown_until:
                continue
            marker = ":" if current == index else "-"
            rows.append(f"{prefix}{current + 1}{marker}{lines[current]}")
            shown_until = current
    return rows, len(found)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def list_entries(directory: Path, pattern: str, recursive: bool = False) -> str:
    """列出一个目录下的条目；``recursive=True`` 时一直展开到最深层（不设层数）。

    头部给出汇总（文件数、目录数、合计字节数）：清点目录不必再去跑 shell 统计。
    """
    rows: list[str] = []
    files = folders = size = 0
    for level_lines, counts in walk(directory, pattern or "*", recursive, prefix=""):
        rows.extend(level_lines)
        files += counts[0]
        folders += counts[1]
        size += counts[2]
    if not files and not folders:
        return f"目录：{directory}（无匹配条目）"
    head = f"目录：{directory}（文件 {files} 个、目录 {folders} 个，合计 {size} 字节）"
    return "\n".join([head, *rows])


def walk(directory: Path, pattern: str, recursive: bool, prefix: str):
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
        # pattern 只过滤文件名：子目录一律入列，否则 depth 递归就没得走了
        if not fnmatch(entry.name, pattern):
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
        yield from walk(child, pattern, True, prefix=f"  {prefix}")


def format_lines(path: str, lines: list[str], offset: Optional[int], limit: Optional[int]) -> str:
    """按行号输出内容，未读完时给出续读提示。"""
    total = len(lines)
    start_at = as_int(offset, "offset", 1)
    if start_at < 1:
        raise RuntimeError(f"offset 需要是正整数（当前：{offset!r}）")
    start = start_at - 1
    if total and start >= total:
        raise RuntimeError(f"起始行号超出文件范围：offset={start_at}，共 {total} 行（{path}）")
    count = as_int(limit, "limit", 0)
    if limit is not None and count <= 0:
        raise RuntimeError(f"limit 需要是正整数（当前：{limit!r}）")
    selected = lines[start : start + count] if count else lines[start:]
    body = "\n".join(f"{start + index + 1}:{line}" for index, line in enumerate(selected))
    end = start + len(selected)
    if end < total:
        return body + f"\n（已截断：返回第 {start + 1}-{end} 行，共 {total} 行；继续读取请用 offset={end + 1}）"
    return body


def as_int(value, name: str, fallback: int) -> int:
    """把模型给的参数转成整数；缺省用 fallback，非法值给中文提示。"""
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} 需要是整数（当前：{value!r}）") from None


