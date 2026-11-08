"""read 工具（看）：定位、浏览、查看内容、搜索过滤。

一个工具覆盖四种读法：

- path 是文件：内容按行输出（带行号，可用 offset/limit 分页）；``.docx``/``.xlsx``/``.pptx`` 由文档库解析为文本
- path 是目录：列出条目（名称、类型、大小、修改时间），可用 pattern 通配过滤、depth 递归
- 文件是 JSON 且给了 ``json_path``：按键/下标只取那一段（如 ``tasks.3.steps[0].goal``），支持 ``*`` 与 ``|``
- 给了 ``grep``：按行检索关键词或正则，返回命中行号与内容；path 是目录时按 depth 跨目录递归检索

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, List, Optional

from peifeng_agent.backend.utils.text import as_text_list, content_lines, image_info, is_image, is_readable

#: 目录递归的最大层数（1 = 只看当前层）
MAX_DEPTH = 3

#: grep 的单次命中上限（再多就缩小关键词）
MAX_GREP_HITS = 500

#: grep 单次最多扫描的文件数（跨目录时文件更多，但仍要有上限）
MAX_GREP_FILES = 2000

#: grep 上下文最多几行
MAX_CONTEXT = 3
#: 不允许全量读的会话记录：读一次就会被写回它自己（越读越大），只能按 json_path 取字段
PROTECTED_JSON = ("detail.json", "summary.json")

#: 一次 paths 批量读的上限：纯本地读取，只防呆（返回量另有 MAX_BATCH_CHARS 兜底）
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）
#: 单次批量返回的总字数上限：批量把"读很多文件"变成一次调用，也必须防它一次灌爆上下文
MAX_BATCH_CHARS = 60_000

#: 路径片段：``a.b[0].c`` → ["a", "b", 0, "c"]
_JSON_TOKEN = re.compile(r"([^.\[\]]+)|\[(-?\d+|\*)\]")

information = {
    "name": "read",
    "description": (
        "看文件内容与目录：①文件 → 带行号全文（.docx/.xlsx/.pptx 自动解析，可 offset/limit 分段）；"
        "②目录 → 条目清单（pattern 过滤文件名、depth 递归层数）；"
        "③.json 加 json_path → 按键/下标取值，支持 * 通配与 | 多路径"
        "（如 json_path=\"tasks.*.requirement|tasks.*.status\" 一次取两列）；"
        "④grep → 按行检索关键词或正则，path 是目录时按 depth 跨目录递归，context 给上下文行；"
        "⑤**detail.json / summary.json 是会话记录，不允许全量读**：读到的内容会作为工具结果写回它自己，"
        "文件越读越大、永远读不完。要查历史就用 json_path 指定字段，"
        "如 json_path=\"tasks.1.steps[0].attempts[-1].result.content\"；"
        "通常也不用查它——本步产出、交接说明、证据位置前面都已经给了。"
        "要多看几个文件时用 paths 一次传完（个数不限），不要一个文件一次调用。"
        "要看一整个目录里所有文件的内容，就用 path + pattern + with_content=true 一次拿完。"
        "用于：确认目录结构、读文件内容、定位关键词、从 JSON 取字段时。"
        "图片文件返回格式、像素尺寸与字节数。相对路径以 <项目目录> 为基准"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录路径（相对或绝对）"},
            "offset": {"type": "integer", "description": "起始行号（1 起；**不传就从第 1 行开始**，仅文件文本）"},
            "limit": {"type": "integer", "description": "最多返回行数（**不传就返回全部，通常不用传**；仅文件文本）"},
            "pattern": {"type": "string", "description": "文件名通配过滤（目录列举与目录检索时生效），如 *.docx"},
            "depth": {"type": "integer", "description": f"目录递归层数（默认 1 只看当前层，最多 {MAX_DEPTH}）"},
            "json_path": {
                "type": "string",
                "description": "JSON 子路径（仅 .json）：点号加下标；* 表示所有键或项（tasks.*.requirement）；"
                "可用 | 一次取多条（tasks.*.a|tasks.*.b）",
            },
            "grep": {
                "type": "string",
                "description": "按行检索的关键词（regex=true 时为正则）：返回命中行号与内容",
            },
            "regex": {"type": "boolean", "description": "grep 按正则表达式匹配"},
            "context": {
                "type": "integer",
                "description": f"grep 命中行的前后各几行上下文（默认 0，最多 {MAX_CONTEXT}）",
            },
            "top": {"type": "integer", "description": "grep 最多返回多少条命中（默认 50）"},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "一次读多个路径（与 path 二选一）：每段结果前带 ===== 路径 =====。"
                    "确实要看多个文件时请一次传进来，不要一个文件一轮"
                ),
            },
            "with_content": {
                "type": "boolean",
                "description": (
                    "path 是目录时，除列出条目外把匹配 pattern 的文件内容也一并返回"
                    "（配合 depth 递归）。要看整个目录的文件内容就用它，一次拿完，别逐个读"
                ),
            },
        },
        "required": [],
    },
}


def read(
    path: str = "",
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    pattern: str = "",
    depth: Optional[int] = None,
    json_path: str = "",
    grep: str = "",
    regex: bool = False,
    context: Optional[int] = None,
    top: Optional[int] = None,
    paths: Optional[List[str]] = None,
    with_content: bool = False,
    base_dir=None,
) -> str:
    """读取文件内容、JSON 子路径、目录条目，或按关键词检索；相对路径以 base_dir 为基准。

    不设人为截断：文档读全文、目录列全部条目、JSON 片段完整给出——读不全会让模型反复换工具重试。

    ``paths`` 一次读多个（每段带 ``===== 路径 =====`` 分隔）：把"一次看几个文件"压成一次调用，
    模型一轮只发一个调用时，轮次就是这么省下来的。
    """
    batch = as_text_list(paths, "paths", MAX_BATCH)
    if batch:
        return read_batch(
            batch,
            offset=offset,
            limit=limit,
            pattern=pattern,
            depth=depth,
            json_path=json_path,
            grep=grep,
            regex=regex,
            context=context,
            top=top,
            base_dir=base_dir,
        )
    if not str(path or "").strip():
        raise RuntimeError("read 需要 path（单个路径）或 paths（多个路径）之一")
    target = resolve(path, base_dir)
    keyword = str(grep or "").strip()
    if keyword:
        keep = as_int(context, "context", 0)
        if keep < 0 or keep > MAX_CONTEXT:
            raise RuntimeError(f"context 需要在 0..{MAX_CONTEXT} 之间（当前：{context!r}）")
        return grep_lines(
            target,
            keyword,
            bool(regex),
            keep,
            as_depth(depth),
            pattern or "*",
            max(1, min(as_int(top, "top", 50), MAX_GREP_HITS)),
            str(path or ""),
        )
    if target.is_dir():
        listing = list_entries(target, pattern, as_depth(depth))
        if not with_content:
            return listing
        # 目录 + with_content：一次把匹配文件的内容也拿走（省掉"列目录 → 逐个读"的 N 轮）
        found = collect_files(target, pattern or "*", as_depth(depth))
        if not found:
            return listing
        body = read_batch([str(item) for item in found], offset=offset, limit=limit, base_dir=None)
        return f"{listing}\n\n{body}"
    if not target.is_file():
        raise RuntimeError(f"路径不存在：{path}")
    if is_image(target):
        return image_info(target)
    trace = str(json_path or "").strip()
    if trace:
        return read_json_path(target, trace)
    # 会话记录（detail.json / summary.json）禁止全量读：读出来的内容本身会被写回这两个文件，
    # 文件越读越大、永远读不完。必须用 json_path 取具体字段。
    if target.name.lower() in PROTECTED_JSON:
        raise RuntimeError(
            f"{target.name} 是全量会话记录，**不能整份读**——读到的内容会被写回它自己，"
            "文件越读越大、永远读不完。请用 json_path 只取需要的字段，例如：\n"
            "  read(path=\"...detail.json\", json_path=\"tasks.1.steps[0].attempts[-1].result.content\")\n"
            "  read(path=\"...detail.json\", json_path=\"tasks.*.status\")   # 支持 * 通配与 | 多路径\n"
            "通常你并不需要它：本步产出、交接说明、关键证据位置都在前面给了。"
        )
    return format_lines(str(target), content_lines(target), offset, limit)


def read_batch(
    items: List[str],
    *,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    pattern: str = "",
    depth: Optional[int] = None,
    json_path: str = "",
    grep: str = "",
    regex: bool = False,
    context: Optional[int] = None,
    top: Optional[int] = None,
    base_dir=None,
) -> str:
    """批量读：逐个走单路径逻辑，按 ``===== 路径 =====`` 分段；单个失败不影响其它。

    串行执行：本地 IO 很快，而"一次调用拿回多份结果"本身就省掉了模型往返，
    再为几个毫秒去并发没有意义。
    """
    blocks: List[str] = []
    total = 0
    for index, item in enumerate(items):
        try:
            body = read(
                item,
                offset=offset,
                limit=limit,
                pattern=pattern,
                depth=depth,
                json_path=json_path,
                grep=grep,
                regex=regex,
                context=context,
                top=top,
                base_dir=base_dir,
            )
        except Exception as error:  # noqa: BLE001 - 单个路径失败不该拖垮整批
            body = f"读取失败：{error}"
        total += len(body)
        if total > MAX_BATCH_CHARS:
            # 批量容易"一次把上下文灌满"：到这里停下并说清还剩哪些没读
            rest = len(items) - index
            blocks.append(
                f"（本批已读 {total} 字，达 {MAX_BATCH_CHARS} 字上限："
                f"剩余 {rest} 个文件未读，请缩小范围、用 offset/limit 分段，或分批再读）"
            )
            break
        blocks.append(f"===== {item} =====\n{body}")
    return "\n\n".join(blocks)


def grep_lines(
    target: Path,
    keyword: str,
    regex: bool,
    context: int,
    depth: int,
    pattern: str,
    top: int,
    shown: str,
) -> str:
    """按行检索：文件内检索，或目录下按 depth 递归检索（pattern 过滤文件名）。"""
    matcher = compile(keyword, regex)
    if target.is_file():
        lines = content_lines(target)
        hits, matched = match_lines(lines, matcher, context)
        if not hits:
            return f"{target} 中未找到 {keyword}（共 {len(lines)} 行）"
        return "\n".join([f"{target} 命中 {matched} 处（共 {len(lines)} 行）", *hits])

    if not target.is_dir():
        raise RuntimeError(f"路径不存在：{shown}")
    hits, matched, scanned, truncated = scan(target, matcher, context, pattern, depth, top)
    if not hits:
        return f"{target} 下未找到 {keyword}（扫描 {scanned} 个文件，递归 {depth} 层）"
    lines = [f"{target} 下命中 {matched} 处（扫描 {scanned} 个文件）", *hits]
    if truncated:
        lines.append("（命中过多已停止：缩小关键词、加 pattern 过滤或降低 depth）")
    return "\n".join(lines)


def scan(root: Path, matcher, context: int, pattern: str, depth: int, top: int) -> tuple[list[str], int, int, bool]:
    """广度优先遍历目录并收集命中，返回 ``(输出行, 命中数, 扫描文件数, 是否被截断)``。

    命中数与输出行分开统计：带 context 时输出行里混着上下文行，不能拿来当命中数。
    """
    hits: list[str] = []
    matched = 0
    scanned = 0
    queue: list[tuple[Path, int]] = [(root, 1)]
    while queue:
        directory, level = queue.pop(0)
        try:
            entries = sorted(Path(directory).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if level < depth:
                    queue.append((entry, level + 1))
                continue
            # pattern 只过滤文件名：不能用 glob(pattern) 取条目，否则子目录也被挡掉、递归失效
            if not fnmatch(entry.name, pattern):
                continue
            if not is_readable(entry):
                continue
            scanned += 1
            if scanned > MAX_GREP_FILES:
                return hits, matched, scanned, True
            try:
                lines = content_lines(entry)
            except Exception:  # noqa: BLE001 - 单个文件读不了就跳过
                continue
            try:
                prefix = f"{entry.relative_to(root).as_posix()}:"
            except ValueError:
                prefix = f"{entry.name}:"
            rows, count = match_lines(lines, matcher, context, prefix=prefix)
            hits.extend(rows)
            matched += count
            if matched >= top:
                return hits, matched, scanned, True
    return hits, matched, scanned, False


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
    for index in found[:MAX_GREP_HITS]:
        first = max(0, index - context)
        last = min(len(lines) - 1, index + context)
        for current in range(first, last + 1):
            if current <= shown_until:
                continue
            marker = ":" if current == index else "-"
            rows.append(f"{prefix}{current + 1}{marker}{lines[current]}")
            shown_until = current
    return rows, min(len(found), MAX_GREP_HITS)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def collect_files(directory: Path, pattern: str, depth: int) -> List[Path]:
    """按 pattern 与 depth 收集文件（与 :func:`list_entries` 的遍历口径一致）。"""
    found: List[Path] = []
    queue: List[tuple] = [(directory, 1)]
    while queue:
        folder, level = queue.pop(0)
        try:
            entries = sorted(Path(folder).iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if level < depth:
                    queue.append((entry, level + 1))
                continue
            if fnmatch(entry.name, pattern) and is_readable(entry):
                found.append(entry)
    return found


def list_entries(directory: Path, pattern: str, depth: int) -> str:
    """列出一个目录下的条目；depth 大于 1 时递归展开子目录。

    头部给出汇总（文件数、目录数、合计字节数）：清点目录不必再去跑 shell 统计。
    """
    rows: list[str] = []
    files = folders = size = 0
    for level_lines, counts in walk(directory, pattern or "*", depth, prefix=""):
        rows.extend(level_lines)
        files += counts[0]
        folders += counts[1]
        size += counts[2]
    if not files and not folders:
        return f"目录：{directory}（无匹配条目）"
    head = f"目录：{directory}（文件 {files} 个、目录 {folders} 个，合计 {size} 字节）"
    return "\n".join([head, *rows])


def walk(directory: Path, pattern: str, depth: int, prefix: str):
    """按层产出 ``(本层条目行, (文件数, 目录数, 合计字节))``；depth 层之后不再下钻。"""
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
    if depth <= 1:
        return
    for child in children:
        yield from walk(child, pattern, depth - 1, prefix=f"  {prefix}")


def read_json_path(target: Path, trace: str) -> str:
    """按 ``a.b[0].c`` 取子值；支持 ``*`` 通配与 ``|`` 多路径，一次拿到一批字段。"""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as error:
        raise RuntimeError(f"这不是合法 JSON，无法按 json_path 读取：{target.name}（{error}）") from None
    traces = [part.strip() for part in str(trace).split("|") if part.strip()]
    if not traces:
        raise RuntimeError("json_path 不能为空")
    if len(traces) == 1:
        return render_json_value(resolve_json_path(payload, traces[0]), traces[0])
    blocks = []
    for one in traces:
        blocks.append(f"--- json_path={one}\n" + render_json_value(resolve_json_path(payload, one), one))
    return "\n\n".join(blocks)


def resolve_json_path(payload: Any, trace: str) -> Any:
    """逐段下钻；键不存在或下标越界时给出可用的键/范围。

    ``*`` 表示这一层的所有键或所有项：``tasks.*.requirement`` 返回 ``{"1": "...", "2": "..."}``。
    """
    return descend(payload, _JSON_TOKEN.findall(trace), trace)


def descend(current: Any, tokens: list, trace: str) -> Any:
    """递归下钻（``*`` 时对每个键或项各展开一份，结果保持原有形状）。"""
    if not tokens:
        return current
    (name, index), rest = tokens[0], tokens[1:]
    if name == "*" or index == "*":
        if isinstance(current, dict):
            return {key: descend(value, rest, trace) for key, value in current.items()}
        if isinstance(current, list):
            return [descend(item, rest, trace) for item in current]
        raise RuntimeError(f"json_path={trace} 的 * 需要作用在对象或数组上，当前是 {type(current).__name__}")
    if index != "":
        if not isinstance(current, list):
            raise RuntimeError(f"json_path={trace} 的 [{index}] 需要作用在数组上，当前是 {type(current).__name__}")
        position = int(index)
        if position < 0:
            # 负下标从末尾数（[-1] 是最后一项）：多次尝试时"最后一次尝试"才是要的正文
            position += len(current)
        if not 0 <= position < len(current):
            raise RuntimeError(f"json_path={trace} 的下标 [{index}] 超出范围（该数组共 {len(current)} 项）")
        return descend(current[position], rest, trace)
    if not isinstance(current, dict):
        raise RuntimeError(f"json_path={trace} 的 .{name} 需要作用在对象上，当前是 {type(current).__name__}")
    if name not in current:
        available = "、".join(list(current)[:12]) or "（无）"
        raise RuntimeError(f"json_path={trace} 里没有 {name}（这一层可用：{available}）")
    return descend(current[name], rest, trace)


def render_json_value(value: Any, trace: str) -> str:
    """字符串按行输出（与文本模式一致），对象与数组按 JSON 输出。"""
    if isinstance(value, str):
        return format_lines(f"json_path={trace}", value.splitlines(), None, None)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        return format_lines(f"json_path={trace}", text.splitlines(), None, None)
    return f"json_path={trace} = {value!r}"


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


def as_depth(value) -> int:
    """目录递归层数：缺省 1，夹在 1..MAX_DEPTH。"""
    depth = as_int(value, "depth", 1)
    if depth < 1:
        raise RuntimeError(f"depth 需要是正整数（当前：{value!r}）")
    return min(depth, MAX_DEPTH)
