"""read 工具（看）：定位、浏览、查看内容、搜索过滤。

一个工具覆盖四种读法：

- path 是文件：内容按行输出（带行号，可用 offset/limit 分页）；``.docx``/``.xlsx``/``.pptx`` 由文档库解析为文本
- path 是目录：列出条目（名称、类型、大小、修改时间），可用 pattern 通配过滤、depth 递归
- 文件是 JSON 且给了 ``json_path``：按键/下标只取那一段（如 ``tasks.3.steps[0].goal``），支持 ``*`` 与 ``|``
- 给了 ``grep``：按行检索关键词或正则，返回命中行号与内容；path 是目录时按 depth 跨目录递归检索

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
        "读文件或列出目录，一个 grep 统一筛选。\n"
        "① 文件：不给 grep → 全文（可 offset/limit 分段）；给关键词或正则 → 只回命中的行；"
        "给 json_path → 按路径取字段（tasks.1.status，支持 * 与 |）。\n"
        "② 目录：不给 grep → 列出条目；给后缀（.md）→ 只列这类文件；"
        "给关键词或正则 → 在文件内容里搜（recursive 时跨子目录）。\n"
        "③ grep 三种写法自动识别：.md 这类是后缀，/正则/ 或含 [ ] ( ) | ^ $ * + ? \\ 是正则，其余是关键词。\n"
        "④ recursive=true 表示**一直往下翻**，不设层数限制。\n"
        "⑤ detail.json / summary.json 是会话记录，**不能整份读**（读到的内容会写回它自己，越读越大），"
        "要用 json_path 指定字段。\n"
        "要紧看几个文件就用 paths 一次传完（个数不限），不要一个文件一次调用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录路径（相对或绝对）"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "一次读多个路径（与 path 二选一），每段结果前带 ===== 路径 ====="},
            "grep": {"type": "string",
                     "description": "筛选条件：关键词 / 后缀(.md) / 正则(/xxx/) 自动识别；不给就是全部"},
            "offset": {"type": "integer", "description": "起始行号（1 起；不传就从第 1 行开始）"},
            "limit": {"type": "integer", "description": "最多返回行数（不传就返回全部）"},
            "recursive": {"type": "boolean", "description": "目录是否递归到底（true = 不限层数）"},
            "json_path": {"type": "string",
                          "description": "从 JSON 按键/下标取值，如 tasks.1.steps[0].goal，支持 * 与 |"},
        },
        "required": [],
    },
}


#: 目录递归时的层数上限：给个很大的值表示"一直往下翻"（不暴露给模型）
_RECURSIVE_DEPTH = 64


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
    path: str = "",
    paths: Optional[List[str]] = None,
    grep: str = "",
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    recursive: bool = False,
    json_path: str = "",
    base_dir=None,
    **_legacy: Any,
) -> str:
    """读文件内容、列出目录、按条件筛选，或从 JSON 取字段。

    两种目标，一套参数：

    - **文件**：``grep`` 给关键词/正则 → 只回命中的行；不给 → 全文（可 ``offset``/``limit`` 分段）。
      ``json_path`` 给了就按路径取值（``tasks.1.status``，支持 ``*`` 与 ``|``）。
    - **目录**：``grep`` 给后缀（``.md``）→ 只列这类文件；给关键词/正则 → 在文件内容里搜。
      ``recursive`` 打开时**一直往下翻**（不限层数），否则只看当前层。

    多个目标用 ``paths`` 一次传完（个数不限）；相对路径以 base_dir（项目目录）为基准。
    """
    # 旧参数兼容：这些键已从 schema 里去掉，但模型偶尔还会按老习惯传；
    # 直接参数不匹配会抛 TypeError 把整步打挂，所以在这里吸收并按新语义映射。
    if _legacy:
        pattern = str(_legacy.get("pattern") or "").strip()
        if pattern and not str(grep or "").strip():
            grep = pattern
        depth = _legacy.get("depth")
        if depth is not None and not recursive:
            try:
                recursive = int(depth) > 1
            except (TypeError, ValueError):
                pass
        if _legacy.get("regex") and str(grep or "").strip():
            grep = f"/{str(grep).strip().strip('/')}/"
    batch = as_text_list(paths, "paths", MAX_BATCH)
    if batch:
        return read_batch(
            batch, offset=offset, limit=limit, json_path=json_path,
            grep=grep, base_dir=base_dir,
        )
    if not str(path or "").strip():
        raise RuntimeError("read 需要 path（单个）或 paths（多个）之一")

    target = resolve(path, base_dir)
    kind, keyword = classify_grep(grep)
    depth = _RECURSIVE_DEPTH if recursive else 1

    if target.is_dir():
        if kind == "suffix":
            # 后缀筛选：列文件（递归与否由 recursive 决定）
            return list_entries(target, f"*{keyword}", depth)
        if kind in ("keyword", "regex"):
            # 内容检索：跨文件搜，命中行带文件名
            return grep_lines(
                target, keyword, kind == "regex", 0, depth, "*",
                max(1, min(as_int(limit, "limit", MAX_GREP_HITS), MAX_GREP_HITS)),
                str(path),
            )
        return list_entries(target, "*", depth)

    if not target.is_file():
        raise RuntimeError(f"路径不存在：{path}")
    if is_image(target):
        return image_info(target)
    trace = str(json_path or "").strip()
    if trace:
        return read_json_path(target, trace)
    if kind in ("keyword", "regex"):
        return grep_lines(target, keyword, kind == "regex", 0, 1, "*",
                          max(1, min(as_int(limit, "limit", MAX_GREP_HITS), MAX_GREP_HITS)),
                          str(path))
    # 会话记录禁止整份读：读到的内容会被写回它自己，越读越大
    if target.name.lower() in PROTECTED_JSON:
        raise RuntimeError(
            f"{target.name} 是全量会话记录，**不能整份读**——读到的内容会被写回它自己，"
            "文件越读越大、永远读不完。请用 json_path 只取需要的字段，例如：\n"
            "  read(path=\"...detail.json\", json_path=\"tasks.1.steps[0].attempts[-1].result.content\")\n"
            "  read(path=\"...detail.json\", json_path=\"tasks.*.status\")"
        )
    return format_lines(str(target), content_lines(target), offset, limit)

def read_batch(
    items: List[str],
    *,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    json_path: str = "",
    grep: str = "",
    base_dir=None,
) -> str:
    """一次读多个路径：每段前带 ``===== 路径 =====``，名称与参数完全一致才合并。"""
    blocks: List[str] = []
    for item in items:
        try:
            body = read(item, offset=offset, limit=limit, json_path=json_path,
                        grep=grep, base_dir=base_dir)
        except RuntimeError as error:
            body = f"错误：{error}"
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
