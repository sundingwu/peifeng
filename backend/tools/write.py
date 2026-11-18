"""write 工具（写）：创建、写入、追加、编辑修改文本文件。

- 给 content 且 append=false：整体写入（不存在则创建、自动创建父目录；**同名文件直接覆盖**，
  旧内容由事务层备份到会话目录，可回溯）
- 给 content 且 append=true：追加到文件末尾（文件不存在时创建）
- 给 edits：`[{oldText, newText}]` 定点修改；每个 oldText 须在原文中精确出现且唯一，区间互不重叠

读写都不做换行转换（LF 保持 LF、CRLF 保持 CRLF），因此 CRLF 文件的 oldText 需自带。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

import re
import json
from typing import Optional

from backend.utils.web import run_targets

from pathlib import Path


#: 一次最多写几个文件


information = {'name': 'write',
 'description': '写文本文件。path必填；正文用content，JSON文件优先用json_data对象/数组，定点修改用edits。三者互斥。步骤终态JSON直接回复，不调用write。使用 '
                'targets 对象数组，每项参数独立生效。',
 'parameters': {'type': 'object',
                'properties': {'targets': {'type': 'array',
                                           'minItems': 1,
                                           'items': {'type': 'object',
                                                     'properties': {'path': {'type': 'string',
                                                                             'description': '文件路径（相对或绝对）'},
                                                                    'json_data': {'anyOf': [{'type': 'object'}, {'type': 'array'}],
                                                                                  'description': '结构化JSON对象或数组，工具负责序列化，无需手动转义；不与content、edits、append或facts混用'},
                                                                    'content': {'type': 'string',
                                                                                'description': '要写入的完整内容（与 '
                                                                                               'edits '
                                                                                               '二选一）；自动建父目录，同名文件直接覆盖，覆盖前把旧版备份到会话目录'},
                                                                    'append': {'type': 'boolean',
                                                                               'description': 'true '
                                                                                              '时追加到文件末尾，默认 '
                                                                                              'false 表示整体写入'},
                                                                    'facts': {'type':'string','description':'可选配对事实JSON；仅整体写入，复算来源后展开content中的{{fact:ID}}，适合直接交付Markdown'},
                                                                    'edits': {'type': 'array',
                                                                              'description': '定点修改列表（与 '
                                                                                             'content '
                                                                                             '二选一），每个匹配原文件而非增量',
                                                                              'items': {'type': 'object',
                                                                                        'properties': {'oldText': {'type': 'string',
                                                                                                                   'description': '原文件中的精确文本（须唯一）'},
                                                                                                       'newText': {'type': 'string',
                                                                                                                   'description': '替换文本'}},
                                                                                        'required': ['oldText',
                                                                                                     'newText']}}},
                                                     'required': ['path'],
                                                     'required': ['path'],
                                                     'additionalProperties': False}}},
                'required': ['targets'],
                'additionalProperties': False}}


def sibling_copy_hint(target: Path) -> str:
    """新建 ``check_fit2.py`` 这类序号副本时提醒复用原文件。

    真实任务里模型曾把同一份校验脚本写成 check_fit.py … check_fit11.py 再逐个跑，
    白烧 11 次调用；这里在写入回执里直接点出来，让它改原文件重跑。
    """
    if target.suffix.lower() != ".py":
        return ""
    stem = target.stem
    match = re.match(r"^(.*?)[-_]?\d{1,3}$", stem)
    if not match:
        return ""
    base = match.group(1)
    if not base or base == stem:
        return ""
    pattern = re.compile(rf"^{re.escape(base)}[-_]?\d{{0,3}}$")
    siblings = sorted(p.name for p in target.parent.glob(f"{base}*.py") if p.name != target.name and pattern.match(p.stem))
    if not siblings:
        return ""
    return f"。提示：同目录已有 {'、'.join(siblings[:3])}，请直接改那个文件重跑，不要新建序号副本。"


def write_target(
    path: str = '',
    content: Optional[str] = None,
    edits: list = None,
    append: bool = False,
    base_dir=None,
    facts: Optional[str] = None,
    json_data=None,
) -> str:
    """整体写入、追加或定点修改一个文件（多个文件用多个 targets，不要用 files）。

    相对路径以 base_dir（项目工作目录）为基准。
    """
    if not str(path or "").strip():
        if isinstance(content, str):
            try:
                final = json.loads(content)
            except ValueError:
                final = None
            if isinstance(final, dict) and 'status' in final and 'summary' in final:
                raise RuntimeError('这是步骤终态JSON，请直接作为最终回复发送，不调用write；只有保存文件才需要path。')
        raise RuntimeError('write 需要 path：用法 write(targets=[{"path":"a.md","content":"正文"}])')
    if json_data is not None:
        if not isinstance(json_data, (dict, list)) or content is not None or edits is not None or append or facts:
            raise ValueError('json_data仅接受对象或数组，不能与content、edits、append或facts混用')
        content = json.dumps(json_data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if (content is None) == (edits is None):
        raise RuntimeError("write 需要且只能提供 content（写入或追加）或 edits（定点修改）之一")
    target = resolve(path, base_dir)
    bound = ''
    if facts:
        if content is None or append:
            raise ValueError('facts仅支持content整体写入，不支持edits或append')
        from backend.utils.skills import load_skill_script
        content, used = load_skill_script('writer','document-formatting','bind_facts').bind_text(content,resolve(facts,base_dir).resolve())
        bound = '；来源复算并绑定事实：' + ', '.join(used) + '。事实已展开存入文件，原{{fact:ID}}占位符不在文件中；后续修改先read实际内容。'
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
        hint = "" if existed else sibling_copy_hint(target)
        return f"{action} {target}（{len(content)} 字符）{bound}{hint}"
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
    """相对路径以 base_dir（项目工作目录）为基准；绝对路径必须落在白名单内。"""
    from backend.utils.paths import resolve_path

    return resolve_path(path, base_dir, write=True)


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


def write(targets: list[dict], base_dir=None, roots=None) -> str:
    """统一批处理入口，每项独立参数。"""
    from backend.utils import paths

    paths.bind(roots or paths.Roots(base_dir))

    return run_targets(lambda target: write_target(**target, base_dir=base_dir), targets, workers=8, base_dir=base_dir)
