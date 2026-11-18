"""read 工具（看）：读文件内容、列目录。

目标类型由路径自己决定，不用另选参数：

- ``path`` 指向文件（可传一组路径批量）：内容按行输出（带行号，可 offset/limit 分段）；
  ``.docx``/``.xlsx``/``.pptx`` 由文档库解析为文本；给 ``grep`` 就只回命中的行
- ``path`` 是目录：列出条目（名称、类型、大小、修改时间）；给后缀（``.md``）只列这类文件，
  给关键词/正则就在其中搜内容，``recursive=True`` 一直往下翻

**不设人为上限**：命中多少给多少、目录多深翻多深、批量多少读多少——
输出过大由工具层的统一兜底处理（超限落盘并把路径给模型），不在这里切。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。

Student与Mentor共享本实现；只读权限由角色可用工具集合控制，无需复制读取逻辑。
"""

from __future__ import annotations

from typing import Optional

from collections import namedtuple
import re
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, List

from backend.utils.web import run_parallel_safe
from backend.utils.text import IMAGE_SUFFIXES, content_lines, image_info, is_image, file_sha256

#: 批量读的并发度：本地读盘，8 路足够快也不会把磁盘打满
READ_WORKERS = 8

information = {
    "name": "read",
    "description": "读取文件或目录，返回SHA256。支持PDF页码、Office正文/结构、图片问答和分页。会话日志用session=true与json_pointer只读所需字段。",
    "parameters": {
        "type": "object",
        "properties": {
            "targets": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "要看的路径：文件→读内容；目录→列出其中的条目"},
                        "question": {"type": "string", "description": "针对图片的问题，默认描述内容和显示异常"},
                        "view": {"type": "string", "enum": ["content", "structure", "records", "metadata"], "description": "默认content；structure读Office/SVG结构；records读表记录；metadata只读文件信息/图片尺寸/PDF页数，不调用视觉模型"},
                        "grep": {"type": "string",
                                 "description": "从这一项的输出里筛出含它的行（不去读别的文件）"},
                        "regex": {"type": "boolean",
                                  "description": "这一项的 grep 是否按正则（默认 false 字面量）"},
                        "offset": {"type": "integer", "description": "这一项从第几行开始（默认 1）"},
                        "limit": {"type": "integer", "description": "这一项最多返回多少行"},
                        "pages": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "PDF指定页码，1起始；省略读取全部页"},
                        "recursive": {"type": "boolean",
                                      "description": "这一项是目录时是否连子目录一起列"},
                        "skill": {"type": "string",
                                  "description": "读本角色某个技能的正文/引用：配合 path（默认 SKILL.md），"
                                                 "例如 {\"skill\": \"data-analysis\", \"path\": \"references/table.md\"}。"
                                                 "项目目录之外只有技能库可以这样读"},
                        "json_pointer": {"type": "string", "description": "JSON字段路径，RFC6901格式，例如 /records/1/steps/0/attempts；只返回该字段。也可用path中的#/字段路径"},
                        "session": {"type": "boolean",
                                    "description": "true 时 path 相对**当前会话目录**（回查自己的明细，"
                                                   "例如 path=summary.json, json_pointer=/records/1/steps/0）"},
                    },
                    "anyOf": [{"required": ["path"]}, {"required": ["skill"]}],
                },
                "description": "要看的全部目标。每项一个对象，参数只对它自己生效；"
                               "结果按顺序一一对应，每段以 ===== 路径 ===== 开头。",
            },
        },
        "required": ["targets"],
        "additionalProperties": False,
    },
}


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
        return [], f'（已到末尾：offset={start}，共 {len(items)} 行；没有更多内容）'
    end = begin + count if count else len(items)
    selected = items[begin:end]
    if end < len(items):
        return selected, f"（已截断：返回第 {begin + 1}-{end} 行，共 {len(items)} 行；继续读取请用 offset={end + 1}）"
    return selected, ""


#: 看一个目标的全部参数：路径 + 只对它生效的筛选与分页
Target = namedtuple(
    "Target",
    "path grep regex offset limit recursive question view pages skill session json_pointer",
    defaults=["", "content", None, "", False, ""],
)


def read(targets: Any = None, base_dir=None, vision=None, roots=None) -> str:
    """看文件内容，或探查目录里有什么。

    ``targets`` 是一组目标，每项自带参数——一个文件的分页、一个目录的递归、
    另一个文件的筛选各按各的来；输出按传入顺序一一对应。

    ``grep`` 只从**这一项的输出**里筛行，不会去读你没指定的文件。
    """
    from backend.utils import paths

    paths.bind(roots or paths.Roots(base_dir))

    entries = normalize(targets)
    if not entries:
        raise RuntimeError('read 需要 targets：[{"path": "..."}, ...]')
    if len(entries) == 1:
        return read_target(entries[0], base_dir=base_dir, vision=vision)
    return read_batch(entries, base_dir=base_dir, vision=vision)


def normalize(targets: Any) -> List[Target]:
    """把 targets 校验成 Target 列表：必须是 ``[{"path": ...}, ...]``。"""
    if targets is None or targets == "":
        return []
    if not isinstance(targets, list):
        raise RuntimeError(f'targets 需要数组：[{{"path": "..."}}]（当前：{type(targets).__name__}）')
    entries: List[Target] = []
    for index, item in enumerate(targets):
        if not isinstance(item, dict):
            raise RuntimeError(f"targets[{index}] 需要对象：{item!r}")
        target = str(item.get("path") or "").strip()
        pointer = str(item.get('json_pointer') or '')
        if '#/' in target:
            target, fragment = target.split('#', 1)
            pointer = pointer or fragment
        skill = str(item.get("skill") or "").strip()
        session = bool(item.get("session", False))
        if not target and not skill:
            raise RuntimeError(f'targets[{index}] 缺少 "path"（或技能正文用 "skill"）：{item!r}')
        if skill:
            session = False  # skill 优先：技能正文走技能库通道
            target = target or "SKILL.md"
        entries.append(Target(
            target,
            str(item.get("grep") or ""),
            bool(item.get("regex", False)),
            item.get("offset"),
            item.get("limit"),
            bool(item.get("recursive", False)),
            str(item.get("question") or ""),
            str(item.get("view") or "content"),
            item.get('pages'),
            skill,
            session,
            pointer,
        ))
    return entries


def read_target(entry: Target, *, base_dir=None, vision=None) -> str:
    """按目标自己的类型分派：目录走 :func:`read_dir`，文件走 :func:`read_file`。"""
    target = resolve_target(entry, base_dir)
    if target.is_dir():
        return read_dir(target, entry)
    if target.is_file():
        return read_file(target, entry, vision=vision)
    raise RuntimeError(f"路径不存在：{entry.path}")


def read_file(target: Path, entry: Target, vision=None) -> str:
    """读文件内容；给了 grep 就只回命中的行，再按 offset/limit 分页。

    图片通过注入的视觉能力读取，未配置时仅提供元信息；Office结构可按需读取。
    """
    digest = file_sha256(target)
    if entry.json_pointer:
        if target.suffix.lower() != '.json' or not entry.json_pointer.startswith('/'):
            raise ValueError('json_pointer需要JSON文件及以/开头的字段路径')
        value = json.loads(target.read_text(encoding='utf-8'))
        try:
            for part in entry.json_pointer[1:].split('/'):
                key = part.replace('~1', '/').replace('~0', '~')
                if isinstance(value, list):
                    if not key.isdigit():
                        raise KeyError(key)
                    value = value[int(key)]
                elif isinstance(value, dict):
                    value = value[key]
                else:
                    raise KeyError(key)
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(f'JSON字段不存在：{entry.json_pointer}') from error
        lines = json.dumps(value, ensure_ascii=False, indent=2).splitlines()
        selected, more = page(lines, entry.offset, entry.limit)
        return f'文件：{target}\nSHA256：{digest}\n字段：{entry.json_pointer}\n' + '\n'.join(selected) + ('\n' + more if more else '')
    if target.name == 'summary.json' and entry.view != 'metadata':
        payload = json.loads(target.read_text(encoding='utf-8'))
        if isinstance(payload, dict) and 'records' in payload:
            return f'文件：{target}\nSHA256：{digest}\n会话日志请按json_pointer读取字段：/tasks（摘要）、/records/<任务号>/steps/<步骤下标>、/records/<任务号>/outputs/<步骤下标>。\n任务号：' + ', '.join(payload['records'])
    if entry.view not in {'content', 'structure', 'records', 'metadata'}:
        raise RuntimeError('view必须为content、structure、records或metadata')
    if entry.pages is not None and (target.suffix.lower() != '.pdf' or entry.view != 'content'):
        raise ValueError('pages仅支持PDF content读取')
    if entry.view == 'metadata':
        body = f'类型：{target.suffix or "无扩展名"}；字节数：{target.stat().st_size}'
        if is_image(target):
            body += '\n' + image_info(target)
        elif target.suffix.lower() == '.pdf':
            from backend.utils.skill_libraries import format_reader
            body += f'\nPDF页数：{format_reader(".pdf").page_count(target)}'
    elif target.suffix.lower() == '.pdf' and entry.pages is not None:
        from backend.utils.skill_libraries import format_reader
        body = shape(format_reader('.pdf').selected_pages(target, entry.pages), entry, str(target))
    elif entry.view == 'records':
        body = shape('\n'.join(record_lines(target)), entry, str(target))
    elif entry.view == 'structure':
        if target.suffix.lower() == '.svg':
            from backend.utils.text import svg_structure
            body = svg_structure(target)
        else:
            from backend.utils.office import office_inspect_target
            body = office_inspect_target(str(target))
    elif is_image(target) or target.suffix.lower() == '.svg':
        if vision is None:
            if entry.question:
                raise RuntimeError('未配置图片理解能力，无法回答图片问题')
            body = image_info(target)
        else:
            body = describe_one(str(target), entry.question or '描述图像内容，指出实际重叠、裁切或乱码；不要推测不可见内容。', 1400, vision, None)
    else:
        body = shape("\n".join(content_lines(target)), entry, str(target))
    if file_sha256(target) != digest:
        raise RuntimeError('读取期间文件发生变化，请重新读取')
    return f'文件：{target}\nSHA256：{digest}\n{body}'



def record_lines(target: Path) -> list[str]:
    """只呈现实际表记录，不执行查询推理或判断声明真伪。"""
    if target.suffix.lower() == '.csv':
        with target.open(encoding='utf-8-sig', newline='') as stream:
            tables = [(target.name, list(csv.reader(stream)))]
    elif target.suffix.lower() == '.xlsx':
        from backend.utils.skill_libraries import format_reader
        tables = format_reader('.xlsx').record_tables(target)
    else:
        raise RuntimeError('records只支持CSV和XLSX；其他材料使用content')
    lines = []
    for sheet, rows in tables:
        if not rows:
            continue
        header = [str(value or '').strip() for value in rows[0]]
        if len(header) != len(set(header)) or not all(header):
            raise RuntimeError('records需要非空且不重复的表头；可用content查看原始表格')
        count = sum(any(value is not None and str(value).strip() for value in row) for row in rows[1:])
        lines.append(f'表：{sheet}；字段：{header}；数据记录数：{count}（不含表头及全空行，未去重）')
        for number, row in enumerate(rows[1:], 2):
            if any(value is not None and str(value).strip() for value in row):
                lines.append(json.dumps({'row': number, 'values': {name: row[index] if index < len(row) else None for index, name in enumerate(header)}}, ensure_ascii=False, default=str))
    return lines


def read_dir(target: Path, entry: Target) -> str:
    """列出目录里的条目；``recursive`` 决定要不要连子目录一起展开（**只列名字，不读内容**）。"""
    return shape(list_entries(target, entry.recursive), entry, str(target))


def shape(body: str, entry: Target, label: str) -> str:
    """两个业务函数共用的收尾：先筛行、再分页、最后统一加行号。"""
    lines = body.splitlines()
    if entry.grep:
        matcher = compile(entry.grep, entry.regex)
        found = [(number, line) for number, line in enumerate(lines, 1) if matcher.search(line)]
        if not found:
            return f"{label} 未找到 {entry.grep!r}（共 {len(lines)} 行）"
        selected, more = page(found, entry.offset, entry.limit)
        rows = [f"{number}:{line}" for number, line in selected]
        head = f"{label} 命中 {len(found)} 处（共 {len(lines)} 行）"
    else:
        selected, more = page(list(enumerate(lines, 1)), entry.offset, entry.limit)
        rows = [f"{number}:{line}" for number, line in selected]
        head = ""
    if more:
        rows.append(more)
    if head:
        return "\n".join([head, *rows])
    return "\n".join(rows) if rows else "（空）"


def read_batch(entries: List[Target], *, base_dir=None, vision=None) -> str:
    """并发看多个目标：每段前带 ``===== 路径 =====``，顺序与传入一致。"""
    results = run_parallel_safe(
        lambda entry: read_target(entry, base_dir=base_dir, vision=vision),
        entries,
        workers=READ_WORKERS,
        thread_prefix="read",
    )
    blocks: List[str] = []
    for entry, (body, error) in zip(entries, results):
        blocks.append(f"===== {entry.path} =====\n{body if error is None else f'错误：{error}'}")
    from backend.utils.tool_output import ToolOutput
    return ToolOutput("\n\n".join(blocks), has_errors=any(error is not None for _, error in results))


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


def resolve_target(entry: Target, base_dir) -> Path:
    """把一项目标解析成路径：技能正文走技能库通道，会话明细走会话通道，其余只认项目目录。"""
    from backend.utils import paths

    if entry.skill:
        return paths.resolve_skill(entry.skill, entry.path)
    if entry.session:
        return paths.resolve_session(entry.path)
    return resolve(entry.path, base_dir)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作目录）为基准；绝对路径必须落在白名单内。"""
    from backend.utils.paths import resolve_path

    return resolve_path(path, base_dir)


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


# ---------------------------------------------------------------- 图像描述实现
"""image_describe 工具：让模型真正看一眼图片并回答针对它的问题。

图片内容（标题有没有乱码、文字有没有溢出、图例对不对得上、截图里是什么界面）
读文本读不出来——本工具把图交给模型的多模态能力，用一句话问出要核对的事。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""





#: 看图并发度：与 VISION_CALL_LIMIT 对齐，再多也没有收益（每张都要过一次模型）


image_information = {'name': 'image_describe',
 'description': '看一眼图片并回答针对它的问题：返回模型对图像内容的文字描述（标题是否完整、文字有没有溢出或重叠、图例与数据对不对得上、截图界面里有什么）。用于核对生成出来的图或渲染出来的页面是否符合预期——图表出完看标题与标签、文档与演示渲染成图后看版面是否错位、读别人给的截图取信息。 '
                '使用 targets 对象数组，每项参数独立生效。',
 'parameters': {'type': 'object',
                'properties': {'targets': {'type': 'array',
                                           'minItems': 1,
                                           'items': {'type': 'object',
                                                     'properties': {'path': {'type': 'string',
                                                                             'description': '图片路径（相对 <项目目录> '
                                                                                            '或绝对路径）'},
                                                                    'question': {'type': 'string',
                                                                                 'description': '针对这张图要回答的问题，如「标题文字是否完整、有无乱码」「正文有没有超出边框」'},
                                                                    'max_side': {'type': 'integer',
                                                                                 'description': '送入模型前把长边缩到多少像素（默认 '
                                                                                                '1400）'}},
                                                     'required': ['path'],
                                                     'additionalProperties': False}}},
                'required': ['targets'],
                'additionalProperties': False}}


def describe_one(path: str, question: str, max_side: int, vision, base_dir) -> str:
    """看一张图并回答。"""
    if vision is None:
        raise RuntimeError("当前角色没有接入看图能力")
    target = image_resolve(path, base_dir)
    if not target.is_file():
        raise RuntimeError(f"图片不存在：{path}")
    if target.suffix.lower() not in IMAGE_SUFFIXES + (".svg",):
        raise RuntimeError(f"只支持图片：{'、'.join(IMAGE_SUFFIXES)}（当前：{target.suffix or '无后缀'}）")
    side = max(400, min(int(max_side or 1400), 2400))
    answer = vision(target, str(question or "").strip() or "描述这张图的内容", side)
    return "\n".join([f"图片：{image_info(target)}", f"问题：{question}", f"回答：{answer.strip()}"])


def image_resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作目录）为基准；绝对路径必须落在白名单内。"""
    from backend.utils.paths import resolve_path

    return resolve_path(path, base_dir)
