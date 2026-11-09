"""docx 工具（写）：创建、追加、改写或定点修改 Word 文档。

工具只管"怎么落进文档"，不预设任何文种风格：

- ``blocks`` 声明内容与结构：标题、标题层级、段落、表格、图片、分页
- ``theme`` 声明风格：页边距、各类段落的字体字号行距缩进对齐、表格样式；**不给就不设字体字号**（工具不带默认样式），参数由技能给出
- ``patch`` 定点修改已有段落（按段号或按文本匹配），保留原样式，不必整篇重写

段落风格（标题层级、正文版式等）由技能定义，技能提供 ``theme`` 的具体数值。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）
#: inspect 最多列多少个段落（长文档防止回执淹没上下文）
MAX_INSPECT_PARAGRAPHS = 80

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from copy import deepcopy
from pathlib import Path
from typing import Optional

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from peifeng_agent.backend.utils import docscript

#: 段落语义：调用方在 blocks 里用这些名字声明这一段是什么
STYLE_NAMES = {
    "title": "标题",
    "heading1": "一级标题",
    "heading2": "二级标题",
    "heading3": "三级标题",
    "body": "正文",
    "caption": "图表题注",
    "signature": "落款",
}

#: 对齐取值 → python-docx 常量
ALIGNS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}

information = {
    "name": "docx",
    "description": (
        "操作 Word 文档（.docx）：blocks 写内容，append 追加（自动继承原文样式）、overwrite 覆盖重写、patch 定点改段落。"
        "用于：生成课程报告、实验报告、总结等 .docx；给已有文档续写；改已有文档的某几段（patch 保留原样式）时。"
        "要改已有文档的某几段，先把 inspect 设为 true 看清段落序号与样式，再用 patch 一次改完。"
        "blocks 每项一个块：{type: title|heading|paragraph|table|image|page_break}——"
        "title 用 text；heading 用 text + level(1|2|3)；paragraph 用 text，可选 style(body|signature|caption) "
        "与 align/font/size/bold/indent/line_spacing 覆盖；table 用 header + rows，可选 widths、header_bold、"
        "header_fill（表头底色，6 位十六进制如 D9E2F3）、stripe（数据行隔行铺浅色）；"
        "image 用 path 与 width_cm；page_break 分页。"
        "theme 声明风格：{page:{top,bottom,left,right}, "
        "styles:{title|heading1..3|body|caption|signature:{font,size,bold,align,indent,line_spacing,space_before,space_after}}, "
        "table:{font,size,header_bold,border,align}}，size 用磅、indent 与 line_spacing 用磅"
        "（行距也可给倍数如 1.5）。**工具本身不带任何默认字体字号**：theme 不给就是无格式（由 Word 默认决定），"
        "版式参数从技能里取（如 summary_docx 的 theme 段），照抄进来。"
        "patch 写法：[{index, text}] 按段号（1 起，与回执清单对应）或 [{match, text}] 按原文片段唯一匹配。"
        "返回结构回执：段落分布、字体字号、页边距、逐段清单，够用来核对版式"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "目标文件（.docx，放 <工作目录> 下）"},
            "blocks": {
                "type": "array",
                "description": "内容块列表，按顺序写入；每项 {type, ...}，type 见工具描述",
                "items": {},
            },
            "theme": {
                "type": "object",
                "description": "风格定义（页边距、各类段落的字体字号行距、表格样式）；不给就无格式，参数从技能取",
            },
            "style": {
                "type": "object",
                "description": (
                    "文档格式（与 theme 同义，优先用这个）：页边距、各类段落的字体字号行距、表格样式；"
                    "参数以技能的 style/theme 段为准——工具本身不带任何默认样式"
                ),
            },
            "inspect": {
                "type": "boolean",
                "description": (
                    "true 时只**读取已有文件**的结构并返回（段落序号/样式/字号/文本、表格行列数与首行），不写文件。"
                    "用 patch 改段落前先看一眼——比写 python 脚本 dump 一遍快得多"
                ),
            },
            "append": {
                "type": "boolean",
                "description": "true 时在已有文件末尾追加（续写用）：不传 theme 时**自动继承上一段的段落与字符格式**，与原文风格一致",
            },
            "overwrite": {"type": "boolean", "description": "true 时覆盖同名文件重写（改稿用）"},
            "patch": {
                "type": "array",
                "description": "定点修改已有段落：[{index, text}] 或 [{match, text}]，保留原样式",
                "items": {},
            },
            "script": {
                "type": "string",
                "description": "对已生成的文件跑一段 python-docx 代码（透传库的全部能力）：变量 doc 是 Document，"
                "另有 Cm/Pt/Inches、WD_ALIGN_PARAGRAPH、WD_LINE_SPACING、RGBColor、qn。"
                "用于加页眉页脚、页码、复杂表格、改某段字体、插入图片等 blocks 表达不了的操作",
            },
        },
        "required": [],
    },
}


information["parameters"]["properties"]["documents"] = {
    "type": "array",
    "description": (
        "一次处理多个目标（与 file 互斥，）：每项是对象，"
        "键与单次调用的参数同名（如 {file: ...}）。要产出多个时用这个，不要一个一次调用"
    ),
    "items": {
        "type": "object",
        # 元素字段 = 单次调用的全部参数（只留类型，不复制描述，控制体积）；
        # 只声明文件名会让模型以为不能传 blocks/slides，生成出空文档
        "properties": {
            key: {"type": spec.get("type", "string")}
            for key, spec in information["parameters"]["properties"].items()
            if key != "documents"
        },
        "required": ["file"],
    },
}


def docx(
    file: str = "",
    blocks: Optional[list] = None,
    theme: Optional[dict] = None,
    style: Optional[dict] = None,
    append: bool = False,
    overwrite: bool = False,
    patch: Optional[list] = None,
    script: str = "",
    inspect: bool = False,
    documents: list = None,
    base_dir=None,
) -> str:
    """写入、追加、覆盖、定点修改、查看结构或跑脚本改 Word 文档，返回落盘回执；相对路径以 base_dir 为基准。"""
    batch = as_dict_list(documents, "documents", MAX_BATCH)
    if batch:
        return call_many(docx, batch, "documents", base_dir=base_dir, preview="file")
    if not str(file or "").strip():
        raise RuntimeError("docx 需要 file（单个）或 documents（多个）之一")
    if inspect:
        return inspect_file(resolve(file, base_dir))
    if script:
        return run_script(resolve(file, base_dir), script)
    target = resolve(file, base_dir)
    if target.suffix.lower() != ".docx":
        raise RuntimeError(f"docx 只支持 .docx（当前：{file}）")
    if patch and (append or overwrite):
        raise RuntimeError("patch 与 append/overwrite 不能同时用：patch 只改指定段落，不重建文档")
    if append and overwrite:
        raise RuntimeError("append 与 overwrite 不能同时为真：追加续写原文件，重写会替换整个文件")
    if not blocks and not patch:
        raise RuntimeError("没有可写入的内容：请给 blocks（写入内容）或 patch（修改段落）")

    spec = merge_theme(style or theme)
    if patch:
        return apply_patch(target, patch, file)

    target.parent.mkdir(parents=True, exist_ok=True)
    replaced = False
    if append:
        if not target.exists():
            raise RuntimeError(f"append=true 需要文件已存在：{file}（首次产出请不要传 append）")
        document = Document(str(target))
    elif target.exists() and not overwrite:
        # 静默改名会让模型以为"没写成功"从而反复重写同一个文件（实测连续写了 20 次），
        # 所以这里直接报错并给出下一步该选的参数。
        raise RuntimeError(
            f"{file} 已存在。要**原地更新**这份文件请传 overwrite=true；"
            f"要在末尾**接着往下写**请传 append=true；"
            f"确实要另存一份副本，就换一个文件名（如 {target.stem}_v2{target.suffix}）。"
        )
    elif overwrite:
        replaced = True
        document = Document()
        apply_page(document, spec)
    else:
        document = Document()
        apply_page(document, spec)

    written = write_blocks(document, blocks, spec, base_dir, inherit=bool(append and not theme))
    document.save(str(target))
    note = "（已覆盖同名文件）" if replaced else ""
    return report(target, written, append, note)


def inspect_file(target: Path) -> str:
    """描述已有 .docx 的结构：段落（序号/样式/字号/文本）与表格（行列数、首行）。

    实现与评审用的 ``office_inspect`` 是同一份，两处看到的完全一致。
    """
    from peifeng_agent.backend.utils.office_inspect import office_inspect

    return office_inspect(path=str(target))


def write_blocks(document, blocks: list, spec: dict, base_dir=None, inherit: bool = False) -> list[dict]:
    """按顺序写入内容块，返回写入记录（供回执清单）。

    ``inherit=True``（续写已有文档且没显式给 theme）：每个新段落都继承**写入前最后一段**
    的段落与字符格式，追加出来的内容与原文风格一致。
    """
    written: list[dict] = []
    for position, block in enumerate(blocks or [], start=1):
        if not isinstance(block, dict):
            raise RuntimeError(f"blocks[{position - 1}] 需要是对象：{{type, ...}}")
        kind = str(block.get("type") or "paragraph").strip().lower()
        if kind == "page_break":
            document.add_page_break()
            written.append({"kind": "分页", "style": "", "text": "—— 分页 ——"})
        elif kind == "table":
            add_table(document, block, spec)
            header = block.get("header") or []
            written.append(
                {
                    "kind": "表格",
                    "style": "",
                    "text": f"{len(header)} 列 × {len(block.get('rows') or [])} 行"
                    + (f"｜{'、'.join(str(item) for item in header[:6])}" if header else ""),
                }
            )
        elif kind == "image":
            name = add_image(document, block, base_dir)
            written.append({"kind": "图片", "style": "", "text": name})
        else:
            style, text = paragraph_spec(block, kind, position)
            # 先取"写入前的最后一段"作格式模板：新段落加进去之后它就是新段落自己了
            source = document.paragraphs[-1] if inherit and document.paragraphs else None
            add_paragraph(document, text, style, block, spec, inherit=source)
            written.append({"kind": "段落", "style": style, "text": text})
    return written


def paragraph_spec(block: dict, kind: str, position: int) -> tuple[str, str]:
    """从内容块解析出 (语义样式, 文本)。"""
    text = str(block.get("text") or "").strip()
    if not text:
        raise RuntimeError(f"blocks[{position - 1}] 缺少 text")
    if kind == "title":
        return "title", text
    if kind == "heading":
        level = int(block.get("level") or 1)
        if level not in (1, 2, 3):
            raise RuntimeError(f"heading 的 level 只支持 1/2/3（当前：{block.get('level')!r}）")
        return f"heading{level}", text
    if kind != "paragraph":
        raise RuntimeError(f"不支持的块类型：{kind}（可用 title/heading/paragraph/table/image/page_break）")
    style = str(block.get("style") or "body").strip()
    if style not in STYLE_NAMES:
        raise RuntimeError(f"paragraph 的 style 只支持 {'/'.join(STYLE_NAMES)}（当前：{style}）")
    return style, text


def merge_theme(theme: Optional[dict]) -> dict:
    """把调用方给的 theme 规范成 ``{page, styles, table}`` —— **不填任何默认字体字号**。

    工具本身不带风格：想有版式就把 ``theme`` 传进来，参数以对应**技能**里的 theme 段为准
    （技能是版式的唯一权威来源）。不传就是"无格式"，由 Word 自己的默认样式决定。
    """
    if not isinstance(theme, dict):
        return {"page": {}, "styles": {}, "table": {}}
    page: dict[str, float] = {}
    for key, value in (theme.get("page") or {}).items():
        try:
            page[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    styles = {
        str(name): dict(values)
        for name, values in (theme.get("styles") or {}).items()
        if isinstance(values, dict)
    }
    # 字体可以写在顶层（对所有样式生效）或每类样式里；支持 {latin, east_asian} 两段式
    raw_font = theme.get("font")
    if isinstance(raw_font, dict):
        default_latin = str(raw_font.get("latin") or raw_font.get("western") or "").strip()
        default_east = str(raw_font.get("east_asian") or raw_font.get("chinese") or "").strip()
    else:
        default_latin = str(raw_font or "").strip()
        default_east = str(theme.get("font_east_asian") or "").strip()
    table = dict(theme.get("table") or {})
    # 只给了字体、没给样式时，让所有段落类型都套上这个字体（字号仍按块或默认决定）
    if (default_latin or default_east) and not styles:
        styles = {name: {} for name in STYLE_NAMES}
    for values in list(styles.values()) + [table]:
        if default_latin and not values.get("font"):
            values["font"] = default_latin
        if default_east and not values.get("font_east_asian"):
            values["font_east_asian"] = default_east
    return {"page": page, "styles": styles, "table": table}


def apply_page(document, spec: dict) -> None:
    """套用页边距（厘米）；theme 没给 page 就保持文档默认，不硬塞数值。"""
    page = spec.get("page") or {}
    if not page:
        return
    section = document.sections[0]
    if page.get("top") is not None:
        section.top_margin = Cm(page["top"])
    if page.get("bottom") is not None:
        section.bottom_margin = Cm(page["bottom"])
    if page.get("left") is not None:
        section.left_margin = Cm(page["left"])
    if page.get("right") is not None:
        section.right_margin = Cm(page["right"])


def add_paragraph(document, text: str, style: str, block: dict, spec: dict, inherit=None) -> None:
    """写入一段：先按 theme 的该类样式，再用块里的显式字段覆盖。

    ``inherit`` 给一段已有段落时，**以它的段落属性与字符属性为底**（续写场景）：
    这样往已有文档里追加一节，字体字号行距与原文完全一致，不必再手工复制 pPr/rPr。
    块里的显式字段仍然优先。
    """
    overrides = {key: block[key] for key in _OVERRIDE_KEYS if key in block}
    merged = {**spec["styles"].get(style, {}), **overrides}
    paragraph = document.add_paragraph()
    run = paragraph.add_run(text)
    if inherit is not None:
        inherit_format(paragraph, run, inherit)
        if overrides:  # 只有显式给了字段才覆盖继承来的格式
            apply_format(paragraph, {**spec["styles"].get(style, {}), **overrides})
            set_font(run, overrides.get("font"), overrides.get("size"), overrides.get("bold"),
             east_asian=overrides.get("font_east_asian"))
        return
    apply_format(paragraph, merged)
    set_font(run, merged.get("font"), merged.get("size"), merged.get("bold"),
             east_asian=merged.get("font_east_asian"))


def inherit_format(paragraph, run, source) -> None:
    """把源段落的段落属性（pPr）与首个非空 run 的字符属性（rPr）复制给新段落。"""
    source_pPr = source._p.pPr
    if source_pPr is not None:
        current = paragraph._p.pPr
        if current is not None:
            paragraph._p.remove(current)
        paragraph._p.insert(0, deepcopy(source_pPr))
    template = next((item for item in source.runs if item.text.strip()), None)
    template_rPr = template._element.rPr if template is not None else None
    if template_rPr is not None:
        current = run._element.rPr
        if current is not None:
            run._element.remove(current)
        run._element.insert(0, deepcopy(template_rPr))


#: 内容块里可以直接覆盖 style 的字段
_OVERRIDE_KEYS = ("font", "size", "bold", "align", "indent", "line_spacing")


def apply_format(paragraph, spec: dict) -> None:
    """套用段落级格式：对齐、缩进、间距、行距。"""
    align = str(spec.get("align") or "").strip().lower()
    if align in ALIGNS:
        paragraph.alignment = ALIGNS[align]
    fmt = paragraph.paragraph_format
    if spec.get("indent") is not None:
        fmt.first_line_indent = Pt(float(spec["indent"]))
    if spec.get("space_before") is not None:
        fmt.space_before = Pt(float(spec["space_before"]))
    if spec.get("space_after") is not None:
        fmt.space_after = Pt(float(spec["space_after"]))
    spacing = spec.get("line_spacing")
    if spacing is not None:
        value = float(spacing)
        if value > 10:  # 大于 10 视为固定磅值（正文常用 28 磅）
            fmt.line_spacing = Pt(value)
            fmt.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        else:  # 小数视为倍数
            fmt.line_spacing = value


def set_font(run, font, size, bold=None, east_asian=None) -> None:
    """设置西文与中文字体、字号、加粗（中文字体走 w:eastAsia）。

    ``font`` 是**西文**字体（ascii/hAnsi），``east_asian`` 是**中文**字体；
    不给 ``east_asian`` 时两者相同（老用法）。
    """
    if size is not None:
        run.font.size = Pt(float(size))
    if bold is not None:
        run.font.bold = bool(bold)
    latin = str(font or "").strip()
    east = str(east_asian or "").strip() or latin
    if not latin and not east:
        return
    if latin:
        run.font.name = latin
    properties = run._element.get_or_add_rPr()
    fonts = properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = properties.makeelement(qn("w:rFonts"), {})
        properties.append(fonts)
    if east:
        fonts.set(qn("w:eastAsia"), east)
    if latin:
        fonts.set(qn("w:ascii"), latin)
        fonts.set(qn("w:hAnsi"), latin)


def add_table(document, block: dict, spec: dict) -> None:
    """写入一个表格：表头 + 数据行，按 theme 统一字体与边框。

    支持单元格底纹：``header_fill`` 给表头铺底色、``stripe`` 让数据行隔行铺浅色
    （颜色取 theme 的 ``table.stripe_fill``，默认浅灰）。这两个能力是真实运行的教训——
    没有它们时，模型为了"表头浅蓝底纹"只能用 ``docx(script=...)`` 手工拼 XML，
    试了 qn / OxmlElement / shade 函数四套写法、连跑 10 次，把一轮的轮次预算烧掉大半。
    """
    header = [str(item) for item in (block.get("header") or [])]
    rows = block.get("rows") or []
    widths = block.get("widths") or spec["table"].get("widths")
    body_width = max((len(header), *(len(row) for row in rows)), default=0)
    if not header and not rows:
        raise RuntimeError("table 需要 header 或 rows")
    columns = body_width or len(header)
    table = document.add_table(rows=len(rows) + (1 if header else 0), cols=columns)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    if spec["table"].get("border", True):
        table.style = "Table Grid"
    header_bold = bool(block.get("header_bold", spec["table"].get("header_bold", True)))
    header_fill = block.get("header_fill", spec["table"].get("header_fill"))
    stripe = bool(block.get("stripe", spec["table"].get("stripe", False)))
    stripe_fill = spec["table"].get("stripe_fill") or "F2F2F2"
    line = 0
    if header:
        for column, value in enumerate(header):
            cell = table.cell(0, column)
            fill_cell(cell, value, spec, bold=header_bold)
            if header_fill:
                shade_cell(cell, header_fill)
        line = 1
    for offset, row in enumerate(rows):
        for column in range(columns):
            value = row[column] if column < len(row) else ""
            cell = table.cell(line + offset, column)
            fill_cell(cell, value, spec, bold=False)
            if stripe and offset % 2 == 1:  # 隔行：第 2、4、6… 行铺底色
                shade_cell(cell, stripe_fill)
    if widths:
        for column, width in enumerate(widths[:columns], start=1):
            for row in table.rows:
                row.cells[column - 1].width = Cm(float(width))


def fill_cell(cell, value, spec: dict, *, bold: bool) -> None:
    """填一个单元格（保留原有段落，只加 run，避免空段叠加）。"""
    paragraph = cell.paragraphs[0]
    align = str(spec["table"].get("align") or "").strip().lower()
    if align in ALIGNS:
        paragraph.alignment = ALIGNS[align]
    run = paragraph.add_run(str(value))
    set_font(run, spec["table"].get("font"), spec["table"].get("size"), bold,
             east_asian=spec["table"].get("font_east_asian"))


def shade_cell(cell, color: str) -> None:
    """给单元格铺底色（``w:shd``）：color 是 6 位十六进制，如 ``D9E2F3``。

    按 OOXML 的元素顺序插入（``w:shd`` 必须排在 ``w:vAlign`` 之前），否则 Word 打开时会
    报"内容有问题"。重复调用只保留最后一次的颜色，不会叠加多个 shd 元素。
    """
    normalized = str(color or "").strip().lstrip("#").upper()
    if len(normalized) != 6 or any(char not in "0123456789ABCDEF" for char in normalized):
        raise RuntimeError(f"底纹颜色要给 6 位十六进制（如 D9E2F3），当前：{color!r}")
    tc_pr = cell._tc.get_or_add_tcPr()
    for existing in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(existing)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), normalized)
    tc_pr.insert_element_before(
        shd,
        "w:noWrap",
        "w:tcMar",
        "w:textDirection",
        "w:tcFitText",
        "w:vAlign",
        "w:hideMark",
    )


def add_image(document, block: dict, base_dir=None) -> str:
    """插入图片，返回回执里显示的说明。"""
    path = str(block.get("path") or "").strip()
    if not path:
        raise RuntimeError("image 需要 path")
    image = resolve(path, base_dir)
    if not image.is_file():
        raise RuntimeError(f"图片不存在：{path}")
    width = block.get("width_cm")
    if width:
        document.add_picture(str(image), width=Cm(float(width)))
    else:
        document.add_picture(str(image))
    align = str(block.get("align") or "center").strip().lower()
    if align in ALIGNS:
        document.paragraphs[-1].alignment = ALIGNS[align]
    return f"{image.name}" + (f"（宽 {float(width):g} 厘米）" if width else "")


def apply_patch(target: Path, patch: list, shown: str) -> str:
    """定点修改已有段落的文本，保留原段落样式。"""
    if not target.is_file():
        raise RuntimeError(f"文件不存在：{shown}")
    document = Document(str(target))
    paragraphs = document.paragraphs
    changed: list[str] = []
    for position, item in enumerate(patch, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"patch[{position - 1}] 需要是对象：{{index, text}} 或 {{match, text}}")
        new_text = str(item.get("text") or "")
        if not new_text.strip():
            raise RuntimeError(f"patch[{position - 1}] 缺少 text")
        if item.get("index") is not None:
            index = int(item["index"])
            if not 1 <= index <= len(paragraphs):
                raise RuntimeError(f"patch[{position - 1}] 的段号超出范围：{index}（文件共 {len(paragraphs)} 段）")
            replace_text(paragraphs[index - 1], new_text)
            changed.append(f"第 {index} 段 → {new_text[:24]}")
            continue
        match = str(item.get("match") or "")
        if not match:
            raise RuntimeError(f"patch[{position - 1}] 需要 index（段号）或 match（原文片段）")
        hits = [paragraph for paragraph in paragraphs if match in paragraph.text]
        if not hits:
            raise RuntimeError(f"patch[{position - 1}] 的 match 未找到：{match[:40]}")
        if len(hits) > 1:
            raise RuntimeError(f"patch[{position - 1}] 的 match 命中 {len(hits)} 段、不唯一：{match[:40]}（改用 index）")
        replace_text(hits[0], new_text)
        changed.append(f"匹配「{match[:16]}」→ {new_text[:24]}")
    document.save(str(target))
    return "\n".join([f"已修改 {len(changed)} 处（{target}）", *[f"  {item}" for item in changed]])


def replace_text(paragraph, new_text: str) -> None:
    """替换段落文本：保留第一个 run 的格式，清掉其余 run。"""
    if not paragraph.runs:
        paragraph.add_run(new_text)
        return
    first = paragraph.runs[0]
    first.text = new_text
    for extra in paragraph.runs[1:]:
        extra._element.getparent().remove(extra._element)


def run_script(target: Path, script: str) -> str:
    """在已存在的文档上执行 python-docx 代码，保存并回报结构变化。"""
    if not target.is_file():
        raise RuntimeError(f"文件不存在：{target.name}（先用 blocks 生成，再用 script 微调）")
    document = Document(str(target))

    def snapshot() -> dict:
        return {"段落数": len(document.paragraphs), "表格数": len(document.tables)}

    before, after, output = docscript.run(
        script,
        {
            "doc": document,
            "Cm": Cm,
            "Pt": Pt,
            "WD_ALIGN_PARAGRAPH": WD_ALIGN_PARAGRAPH,
            "WD_LINE_SPACING": WD_LINE_SPACING,
            "qn": qn,
        },
        lambda: document.save(str(target)),
        snapshot,
    )
    lines = [f"已执行脚本（{target.name}）", f"结构：{docscript.diff(before, after)}"]
    if output.strip():
        lines.append("脚本输出：")
        lines.extend(f"  {line}" for line in output.strip().splitlines()[:20])
    return "\n".join(lines)


def report(target: Path, written: list[dict], append: bool, note: str) -> str:
    """落盘回执：核对版式要看的信息一次给全。"""
    document = Document(str(target))
    total = len(document.paragraphs)
    empty = sum(1 for paragraph in document.paragraphs if not paragraph.text.strip())
    section = document.sections[0]
    counts: dict[str, int] = {}
    for item in written:
        if item["kind"] == "段落":
            counts[item["style"]] = counts.get(item["style"], 0) + 1
    tables = sum(1 for item in written if item["kind"] == "表格")
    images = sum(1 for item in written if item["kind"] == "图片")
    lines = [
        f"已{'追加' if append else '创建'} {target}{note}",
        f"本次写入 {len(written)} 块，文件共 {total} 段（其中空段 {empty}）"
        + (f"、表格 {tables} 个" if tables else "")
        + (f"、图片 {images} 张" if images else ""),
    ]
    if counts:
        lines.append("段落分布：" + "、".join(
            f"{STYLE_NAMES.get(style, style)} {count} 段" for style, count in counts.items()
        ))
    lines.append(
        f"页边距：上 {section.top_margin.cm:.1f} / 下 {section.bottom_margin.cm:.1f} / "
        f"左 {section.left_margin.cm:.1f} / 右 {section.right_margin.cm:.1f} 厘米"
    )
    lines.append(f"逐段清单（本次写入 {len(written)} 块）：")
    for index, item in enumerate(written[:40], start=1):
        label = f"{STYLE_NAMES.get(item['style'], item['style'])}｜" if item["style"] else ""
        lines.append(f"  {index}. {item['kind']}｜{label}{' '.join(str(item['text']).split())[:24]}")
    if len(written) > 40:
        lines.append(f"  …（其余 {len(written) - 40} 块略）")
    return "\n".join(lines)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def avoid_overwrite(target: Path) -> tuple[Path, bool]:
    """同名文件已存在时另存为 ``name-1.docx``、``name-2.docx``…，返回 (实际路径, 是否改名)。

    只用于"确实要另存一份"的调用方；``docx()`` 主路径不再静默改名——那样模型会以为
    自己没写成功，反复重写同一个文件（实测连写 20 次把轮次烧光），改成报错并指路了。
    """
    if not target.exists():
        return target, False
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate, True
    raise RuntimeError(f"同名文件过多，请换一个文件名：{target.name}")
