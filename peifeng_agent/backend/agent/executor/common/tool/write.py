"""write 工具（写）：创建、写入、追加、编辑修改文本文件。

- 给 content 且 append=false：整体写入（不存在则创建、自动创建父目录；**同名文件直接覆盖**，
  旧内容由事务层备份到会话目录，可回溯）
- 给 content 且 append=true：追加到文件末尾（文件不存在时创建）
- 给 edits：`[{oldText, newText}]` 定点修改；每个 oldText 须在原文中精确出现且唯一，区间互不重叠

读写都不做换行转换（LF 保持 LF、CRLF 保持 CRLF），因此 CRLF 文件的 oldText 需自带。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from peifeng_agent.backend.utils.text import as_dict_list, call_many

#: 一次最多写几个文件
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

information = {
    "name": "write",
    "description": (
        "写文本文件：整体写入、追加、或按原文定点修改（三种方式互斥）。"
        "用于生成 .md/.txt/.csv 等文本产出、给日志追加内容、改已有文本的某几处。"
        "读写不做换行转换。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对或绝对）"},
            "content": {"type": "string",
                        "description": "要写入的完整内容（与 edits 二选一）；自动建父目录，同名文件直接覆盖，覆盖前把旧版备份到会话目录"},
            "append": {"type": "boolean", "description": "true 时追加到文件末尾，默认 false 表示整体写入"},
            "edits": {
                "type": "array",
                "description": "定点修改列表（与 content 二选一），每个匹配原文件而非增量",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {"type": "string", "description": "原文件中的精确文本（须唯一）"},
                        "newText": {"type": "string", "description": "替换文本"},
                    },
                    "required": ["oldText", "newText"],
                },
            },
            "files": {
                "type": "array",
                "description": (
                    f"一次写多个文件（与 path/content 互斥，）："
                    "每项 {path, content} 或 {path, edits} 或 {path, content, append:true}。"
                    "要产出好几个文件时用这个，不要一个文件一次调用"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "content": {"type": "string", "description": "文件内容（整体写入）"},
                        "edits": {"type": "array", "items": {"type": "object"}, "description": "定点修改"},
                        "append": {"type": "boolean", "description": "true 时追加"},
                    },
                    "required": ["path"],
                },
            },
        },
        "required": [],
    },
}


def write(
    path: str = "",
    content: Optional[str] = None,
    edits: list = None,
    append: bool = False,
    files: list = None,
    base_dir=None,
) -> str:
    """整体写入、追加或定点修改文件；``files`` 可一次写多个。

    相对路径以 base_dir（项目工作文件夹）为基准。
    """
    batch = as_dict_list(files, "files", MAX_BATCH)
    if batch:
        return call_many(write, batch, "files", base_dir=base_dir, preview="path")
    if not str(path or "").strip():
        raise RuntimeError("write 需要 path（单个文件）或 files（多个文件）之一")
    if (content is None) == (edits is None):
        raise RuntimeError("write 需要且只能提供 content（写入或追加）或 edits（定点修改）之一")
    target = resolve(path, base_dir)
    if content is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if append:
            existed = target.is_file()
            with open(target, "a", encoding="utf-8", newline="") as handle:
                handle.write(content)
            action = "已追加到" if existed else "已创建"
            return f"{action} {target}（{len(content)} 字符）"
        # 整体写入直接覆盖：覆盖前文件会被事务层备份到会话目录（可回溯），
        # 所以不再另存 name-1.ext —— 那会让"重写同一个脚本"变成一堆副本，
        # 也会让两个会话同时写同一个文件时各写各的、谁都不生效。
        existed = target.is_file()
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        action = "已覆盖" if existed else "已写入"
        return f"{action} {target}（{len(content)} 字符）"
    pairs = parse_edits(edits)
    if not target.exists():
        raise RuntimeError(f"文件不存在：{path}")
    with open(target, encoding="utf-8", newline="") as handle:
        original = handle.read()
    # 先算好结果再打开文件：locate 抛错时不能把原文件截断
    edited = rebuild(original, locate(original, pairs))
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(edited)
    return f"已修改 {len(pairs)} 处（{target}）"


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir 为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target
    raise RuntimeError(f"同名文件过多，请换一个文件名：{target.name}")


def parse_edits(edits: list) -> list[tuple[str, str]]:
    """校验 edits 结构，返回 (oldText, newText) 列表。"""
    if not isinstance(edits, list) or not edits:
        raise RuntimeError("edits 需要非空数组：[{oldText, newText}]")
    pairs: list[tuple[str, str]] = []
    for item in edits:
        if not isinstance(item, dict) or not isinstance(item.get("oldText"), str) or not isinstance(item.get("newText"), str):
            raise RuntimeError(f"edits 元素需为 {{oldText, newText}}：{item}")
        if not item["oldText"]:
            raise RuntimeError("edits[].oldText 不能为空")
        pairs.append((item["oldText"], item["newText"]))
    return pairs


def locate(original: str, pairs: list[tuple[str, str]]) -> list[tuple[int, int, str]]:
    """定位每个 oldText 在原文中的唯一区间，并校验区间互不重叠。"""
    spans: list[tuple[int, int, str]] = []
    for old_text, new_text in pairs:
        count = original.count(old_text)
        if count == 0:
            hint = "（文件用 CRLF 换行，oldText 里的换行需与之一致）" if "\r\n" in original else ""
            raise RuntimeError(f"oldText 未在文件中找到：{old_text[:80]}{hint}")
        if count > 1:
            raise RuntimeError(f"oldText 在文件中出现 {count} 次、不唯一：{old_text[:80]}（请扩大上下文使其唯一）")
        start = original.find(old_text)
        spans.append((start, start + len(old_text), new_text))
    spans.sort(key=lambda span: span[0])
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            raise RuntimeError("edits 存在重叠或嵌套 oldText，请合并为一次替换")
    return spans


def rebuild(original: str, spans: list[tuple[int, int, str]]) -> str:
    """按已排序且互不重叠的区间一次性重建文本。"""
    pieces: list[str] = []
    cursor = 0
    for start, end, new_text in spans:
        pieces.append(original[cursor:start])
        pieces.append(new_text)
        cursor = end
    pieces.append(original[cursor:])
    return "".join(pieces)
