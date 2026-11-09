"""pptx 工具（写）：创建、追加、覆盖演示文稿，支持多种页型、图片与演讲备注。

工具只管"怎么落进幻灯片"，不预设任何版式风格：

- ``slides`` 声明每页：页型、标题、副标题、要点、左右两栏、图片、备注
- ``theme`` 声明风格：配色、字体、正文字号档位；**从空白画时必须给**（缺 colors 报错），套模板时不需要

演示风格（校园汇报、述职、说课等）由技能定义，技能提供 ``theme`` 与页型搭配。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）
#: inspect 每页最多列多少个对象（一页塞几十个文本框时防止回执淹没上下文）
MAX_INSPECT_SHAPES = 40

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from copy import deepcopy
from pathlib import Path
from typing import Optional

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Cm, Emu, Inches, Pt

from peifeng_agent.backend.utils import docscript

#: 16:9 画布尺寸（英寸）
SLIDE_WIDTH = 13.333
SLIDE_HEIGHT = 7.5

#: 锚点清单里单个锚点显示多少个字（超出截断，replace 按子串匹配仍可用前缀）
ANCHOR_SHOWN = 26
#: 每页最多列多少个锚点
ANCHOR_LIMIT = 8

#: 页型说明（回执里用）
TYPE_NAMES = {
    "cover": "封面",
    "section": "章节页",
    "content": "内容页",
    "two_column": "左右两栏",
}

information = {
    "name": "pptx",
    "description": (
        "操作演示文稿（.pptx）：slides 写页，append 追加、overwrite 覆盖重写；给 template 时按模板页复制版式。"
        "用于：做工作汇报、述职、说课、答辩等演示；需要封面、章节页或两栏对比页；"
        "要按已有模板（如项目答辩 27 页蓝白模板）逐页套版时。"
        "两种做法：①从空白画——slides 每项 {type, title, subtitle, bullets, left, right, image, notes}，"
        "type 取 cover(封面)/section(章节页)/content(内容页)/two_column(左右两栏)，"
        "theme 声明 {font, colors:{...}, body_sizes}；"
        "②套模板——template 给模板路径，slides 每项 {template_page, replace, drop, images, notes}："
        "template_page 是模板第几页（1 起），replace 按文本锚点换文案 {旧文本: 新文本} 并保留原字体字号，"
        "命中锚点的段落整段换成新文本（不是段内替换），同一占位要分别填时把新文本写成数组、按出现顺序依次填，"
        "drop 列出要清掉的占位文本（如 照片、xxxxxxxx），images 追加图片 [{path, left_cm, top_cm, width_cm}]。"
        "想先看模板每页有什么可换的文本锚点，把 describe_template 设为 true；"
        "要改**已有文件**的版面，先把 inspect 设为 true 看清每页对象的位置/尺寸/字号，再一次改完。"
        "返回结构回执：每页类型、标题、要点条数、图片与备注情况"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "目标文件（.pptx，放 <工作目录> 下）"},
            "slides": {
                "type": "array",
                "description": "页列表，每项 {type, title, subtitle, bullets, left, right, image, notes}",
                "items": {},
            },
            "template": {
                "type": "string",
                "description": "模板文件路径（.pptx，绝对路径或相对 <工作目录>）；给了就按模板页复制版式",
            },
            "describe_template": {
                "type": "boolean",
                "description": "true 时只返回模板每页的对象数与文本锚点，不生成文件（先看清模板再动手）",
            },
            "inspect": {
                "type": "boolean",
                "description": (
                    "true 时只**读取已有文件**的结构并返回（页数、每页每个形状的类型/文本/位置/字号/尺寸），不写文件。"
                    "改版面、调字号、查某个对象在哪之前先看一眼——比写 python 脚本 dump 一遍快得多"
                ),
            },
            "script": {
                "type": "string",
                "description": "对已生成的文件跑一段 python-pptx 代码（透传库的全部能力）：变量 prs 是 Presentation，"
                "另有 slides、Cm/Emu/Inches/Pt、RGBColor、MSO_SHAPE、PP_ALIGN、qn。"
                "用于模板里没有的版式（如自建对照表格）、精细调字号与位置、删多余对象。"
                "**改字号要同时改两处**：run.font.size 与段落的 endParaRPr（段末标记）——"
                "行高按段末算。段末要跟随本段字号（大标题别压成 14），不是统一一个值",
            },
            "theme": {
                "type": "object",
                "description": "从空白画时**必须**给：{font, colors:{primary,secondary,light,accent,white}, body_sizes}；"
                "缺 colors 会报错。套模板（template）时不需要，样式来自模板页",
            },
            "style": {
                "type": "object",
                "description": (
                    "文档格式（与 theme 同义，优先用这个）：字体、字号、配色、页边距、行距等；"
                    "参数以技能的 style/theme 段为准——工具本身不带任何默认样式"
                ),
            },
            "append": {"type": "boolean", "description": "true 时在已有文件末尾追加页（分批产出用）"},
            "offset": {"type": "integer", "description": "已有页数（追加时的页码起点）"},
            "total": {"type": "integer", "description": "总页数（用于页码显示）"},
            "overwrite": {"type": "boolean", "description": "true 时覆盖同名文件重写（改稿用）"},
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


def pptx(
    file: str = "",
    slides: Optional[list] = None,
    template: str = "",
    describe_template: bool = False,
    inspect: bool = False,
    script: str = "",
    theme: Optional[dict] = None,
    style: Optional[dict] = None,
    append: bool = False,
    offset: int = 0,
    total: int = 0,
    overwrite: bool = False,
    documents: list = None,
    base_dir=None,
) -> str:
    """写入、追加或覆盖演示文稿，返回落盘回执；相对路径以 base_dir 为基准。"""
    batch = as_dict_list(documents, "documents", MAX_BATCH)
    if batch:
        return call_many(pptx, batch, "documents", base_dir=base_dir, preview="file")
    if not str(file or "").strip():
        raise RuntimeError("pptx 需要 file（单个）或 documents（多个）之一")
    if describe_template:
        if not template:
            raise RuntimeError("describe_template=true 时需要同时给 template（模板文件路径）")
        return describe_template_info(resolve(template, base_dir))
    if inspect:
        return inspect_file(resolve(file, base_dir))
    if script:
        return run_script(resolve(file, base_dir), script)

    target = resolve(file, base_dir)
    if target.suffix.lower() != ".pptx":
        raise RuntimeError(f"pptx 只支持 .pptx（当前：{file}）")
    if not slides:
        raise RuntimeError("没有可写入的内容：请给 slides（[{type, title, bullets}] 或 [{template_page, replace}]）")
    if append and overwrite:
        raise RuntimeError("append 与 overwrite 不能同时为真：追加续写原文件，重写会替换整个文件")
    if template:
        return from_template(file, target, slides, template, append, overwrite, base_dir)

    spec = merge_theme(style or theme)
    target.parent.mkdir(parents=True, exist_ok=True)
    if append:
        if not target.exists():
            raise RuntimeError(f"append=true 需要文件已存在：{file}（首次产出请不要传 append）")
        presentation = Presentation(str(target))
        replaced, renamed = False, False
    elif overwrite and target.exists():
        presentation = Presentation()
        replaced, renamed = True, False
    else:
        target, renamed = avoid_overwrite(target)
        presentation = Presentation()
        replaced = False
    presentation.slide_width = Inches(SLIDE_WIDTH)
    presentation.slide_height = Inches(SLIDE_HEIGHT)

    start = int(offset or 0)
    total = int(total or 0) or (start + len(slides))
    for position, page in enumerate(slides):
        add_slide(presentation, start + position + 1, page, total, spec, base_dir)
    presentation.save(str(target))

    if replaced:
        note = "（已覆盖同名文件）"
    else:
        note = f"（同名文件已存在，为避免覆盖另存为 {target.name}）" if renamed else ""
    return report(target, slides, total, append, note, spec)


def from_template(
    file: str,
    target: Path,
    slides: list,
    template: str,
    append: bool,
    overwrite: bool,
    base_dir,
) -> str:
    """按模板页逐页复制版式、替换文案，落盘并回执。"""
    source = resolve(template, base_dir)
    if not source.is_file():
        raise RuntimeError(f"模板不存在：{template}")
    if source.suffix.lower() != ".pptx":
        raise RuntimeError(f"模板需要是 .pptx（当前：{template}）")
    template_book = Presentation(str(source))
    if not template_book.slides:
        raise RuntimeError(f"模板里没有页面：{template}")

    target.parent.mkdir(parents=True, exist_ok=True)
    replaced = False
    if append:
        if not target.exists():
            raise RuntimeError(f"append=true 需要文件已存在：{file}（首次产出请不要传 append）")
        presentation = Presentation(str(target))
        renamed = False
    elif overwrite and target.exists():
        presentation = Presentation()
        replaced, renamed = True, False
    else:
        target, renamed = avoid_overwrite(target)
        presentation = Presentation()
    if not append:
        presentation.slide_width = template_book.slide_width
        presentation.slide_height = template_book.slide_height

    total = len(presentation.slides) + len(slides)
    written: list[dict] = []
    for position, page in enumerate(slides, start=1):
        if not isinstance(page, dict):
            raise RuntimeError(f"第 {position} 页需要是对象：{{template_page, replace}}")
        page_no = int(page.get("template_page") or 0)
        if not 1 <= page_no <= len(template_book.slides):
            raise RuntimeError(
                f"第 {position} 页的 template_page={page.get('template_page')!r} 超出范围"
                f"（模板共 {len(template_book.slides)} 页）"
            )
        slide = copy_slide(template_book.slides[page_no - 1], presentation)
        changed = replace_texts(slide, page.get("replace") or {})
        dropped = drop_shapes(slide, page.get("drop") or [])
        pictures = append_images(slide, page.get("images") or [], base_dir)
        notes = str(page.get("notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
        written.append(
            {
                "page_no": page_no,
                "index": len(presentation.slides),
                "changed": changed,
                "dropped": dropped,
                "pictures": pictures,
                "notes": notes,
                "texts": len(slide_texts(slide)),
            }
        )

    presentation.save(str(target))
    if replaced:
        note = "（已覆盖同名文件）"
    else:
        note = f"（同名文件已存在，为避免覆盖另存为 {target.name}）" if renamed else ""
    return template_report(target, written, total, append, note, source, template_book)


def copy_slide(source_slide, presentation):
    """把模板页的对象、背景与图片关系复制到新演示文稿的一张空白页。"""
    blank = next(
        (layout for layout in presentation.slide_layouts if layout.name in ("Blank", "空白")),
        presentation.slide_layouts[6],
    )
    slide = presentation.slides.add_slide(blank)
    for shape in list(slide.shapes):
        shape._element.getparent().remove(shape._element)
    background = source_slide._element.find(qn("p:cSld")).find(qn("p:bg"))
    if background is not None:
        slide._element.find(qn("p:cSld")).insert(0, deepcopy(background))
    for shape in source_slide.shapes:
        slide.shapes._spTree.append(deepcopy(shape._element))
    # 关系要**全部复制并沿用源页的 rId**：deepcopy 过来的图形里写的是 r:embed="rId7" 这类引用，
    # 只补图片类关系时，图表、OLE、超链接的引用就悬空了（读回报 no relationship with key 'rId15'）。
    for relation in source_slide.part.rels.values():
        if relation.reltype.endswith("/slideLayout"):
            continue  # 新页自带的版式关系，重复添加会冲突
        try:
            slide.part.rels.add_relationship(
                relation.reltype,
                relation._target,
                relation.rId,
                external=relation.is_external,
            )
        except Exception:  # noqa: BLE001 - 个别关系复制失败不影响版式
            continue
    return slide


def iter_text_shapes(shapes):
    """递归取出所有带文本的对象（含组合内的子对象）。"""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from iter_text_shapes(shape.shapes)
        elif getattr(shape, "has_text_frame", False):
            yield shape


def slide_texts(slide) -> list[str]:
    """该页所有段落文本（一行一段，压掉多余空白），用于回执与锚点说明。

    按**段落**取而不是按对象合并：replace 就是按段落匹配的，锚点清单该与它同口径，
    否则模型会拿合并后的长串去当键、替换不上。
    """
    texts = []
    for shape in iter_text_shapes(slide.shapes):
        for paragraph in shape.text_frame.paragraphs:
            text = " ".join(paragraph.text.split())
            if text:
                texts.append(text)
    return texts


def replace_texts(slide, mapping: dict) -> int:
    """按锚点**整段替换**文案，保留该段第一个 run 的字体字号。

    语义是"这一整段换成新文本"而不是"段内替换这几个字"：模板占位往往是一长串
    占位文案（您的内容打在这里…），用前缀做键时按段内替换会留下半截占位文字。

    ``mapping`` 的值给字符串时该锚点命中的段落都换成它；给数组时按出现顺序依次填
    ——模板里同一占位（如六个「添加标题」）各写各的场合用它。
    """
    if not isinstance(mapping, dict) or not mapping:
        return 0
    paragraphs = [
        paragraph
        for shape in iter_text_shapes(slide.shapes)
        for paragraph in shape.text_frame.paragraphs
    ]
    changed = 0
    for old, new in mapping.items():
        key = str(old)
        if not key:
            continue
        targets = [item for item in paragraphs if key in item.text]
        if not targets:
            continue
        if isinstance(new, (list, tuple)):
            for paragraph, value in zip(targets, new):
                rewrite_paragraph(paragraph, str(value))
                changed += 1
            continue
        for paragraph in targets:
            rewrite_paragraph(paragraph, str(new))
            changed += 1
    return changed


def rewrite_paragraph(paragraph, text: str) -> None:
    """改写段落文本：文本写进第一个 run，其余 run 清掉，格式保持。

    新文本为空串时把这一段整体删掉（模板里有些占位段要清空而不是留空行）。
    """
    if not text.strip():
        element = paragraph._p
        element.getparent().remove(element)
        return
    runs = paragraph.runs
    if not runs:
        paragraph.text = text
        return
    runs[0].text = text
    for extra in runs[1:]:
        element = extra._r  # python-pptx 的 run 元素是 _r
        element.getparent().remove(element)


def drop_shapes(slide, texts: list) -> int:
    """删除占位对象：文本与给定值相同、或包含给定片段的对象（清掉模板占位与无关英文）。"""
    wanted = [str(item).strip() for item in texts if str(item).strip()]
    if not wanted:
        return 0
    dropped = 0
    for shape in list(slide.shapes):
        if drop_from(shape, wanted):
            dropped += 1
    return dropped


def drop_from(shape, wanted: set) -> bool:
    """递归删除：组合内全部子对象都在候选中时删整个组合，否则逐个删。"""
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        children = list(shape.shapes)
        if children and all(matches(shape_item, wanted) for shape_item in children):
            shape._element.getparent().remove(shape._element)
            return True
        removed = any(drop_from(shape_item, wanted) for shape_item in children)
        return removed
    if matches(shape, wanted):
        shape._element.getparent().remove(shape._element)
        return True
    return False


def matches(shape, wanted: list) -> bool:
    """对象的文本（压成单行）等于某个给定值、或包含某个给定片段时命中。"""
    if not getattr(shape, "has_text_frame", False):
        return False
    text = " ".join(shape.text_frame.text.split())
    return bool(text) and any(text == item or item in text for item in wanted)


def append_images(slide, images: list, base_dir) -> list[str]:
    """在页面上追加图片；给了 left_cm/top_cm 就按厘米精确定位。"""
    placed: list[str] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            raise RuntimeError("images 项需要 path")
        source = resolve(path, base_dir)
        if not source.is_file():
            raise RuntimeError(f"图片不存在：{path}")
        width = item.get("width_cm")
        left = item.get("left_cm")
        top = item.get("top_cm")
        kwargs = {}
        if width:
            kwargs["width"] = Cm(float(width))
        if left is not None and top is not None:
            kwargs["left"] = Cm(float(left))
            kwargs["top"] = Cm(float(top))
        slide.shapes.add_picture(str(source), **({"left": Cm(float(left or 0)), "top": Cm(float(top or 0))} | kwargs))
        placed.append(source.name + (f"（宽 {float(width):g} 厘米）" if width else ""))
    return placed


def slot_rows(slide, book_width, book_height) -> list[dict]:
    """该页每个文本槽位的位置、字号、可用宽度与字数上限。

    模板每页的槽位散在四周、字号各不相同：不报位置与容量，套版时就会把长句塞进
    4 厘米宽的标签位、或者把说明填到标题框里。
    """
    rows: list[dict] = []
    for shape in iter_text_shapes(slide.shapes):
        text = " ".join(shape.text_frame.text.split())
        if not text:
            continue
        size = next(
            (
                run.font.size.pt
                for paragraph in shape.text_frame.paragraphs
                for run in paragraph.runs
                if run.font.size
            ),
            None,
        )
        width_cm = Emu(shape.width).cm if shape.width else 0
        rows.append(
            {
                "text": text,
                "pos": where(shape.left, shape.top, book_width, book_height),
                "size": size,
                "width": width_cm,
                # 中文字宽约等于字号：只报每行能放几个字，行数由写的人自己控制
                "per_line": per_line(shape, size),
            }
        )
    return rows


def where(left, top, book_width, book_height) -> str:
    """EMU 坐标 → 版面方位词（左上 / 中右 / 下中…）。"""
    if left is None or top is None or not book_width or not book_height:
        return "?"
    x = left / book_width
    y = top / book_height
    col = "左" if x < 0.34 else ("右" if x > 0.62 else "中")
    row = "上" if y < 0.22 else ("下" if y > 0.62 else "中")
    if row == "中" and col == "中":
        return "正中"
    return f"{row}{col}" if row != "中" else f"中{col}"


def per_line(shape, size) -> int:
    """这个槽位一行能放几个字（中文按字号约等于字宽估算）。"""
    if not size or not shape.width:
        return 0
    return max(1, int(Emu(shape.width).pt / size))


def inspect_file(target: Path) -> str:
    """描述已有 .pptx 的结构：每页每个形状的类型、文本、位置、尺寸、字号。

    实现与评审用的 ``office_inspect`` 是同一份（GROUP 会递归展开、自动缩放与母版字号都会标出）；
    给"改版面"用——知道每个对象在哪、多大、什么字号，就能一次把修复脚本写对。
    """
    from peifeng_agent.backend.utils.office_inspect import office_inspect

    return office_inspect(path=str(target))


def describe_shape(shape) -> str:
    """一个形状的一行摘要：类型 · 位置与尺寸（厘米）· 字号 · 文本。"""
    try:
        box = (
            f"{Emu(shape.left).cm:.1f},{Emu(shape.top).cm:.1f} "
            f"{Emu(shape.width).cm:.1f}×{Emu(shape.height).cm:.1f}"
        )
    except (TypeError, ValueError):
        box = "位置未知"
    kind = "图片" if shape.shape_type == MSO_SHAPE_TYPE.PICTURE else str(shape.shape_type or "对象").split(" (")[0]
    if getattr(shape, "has_table", False) and shape.has_table:
        table = shape.table
        return f"{kind} [{box}] 表格 {len(table.rows)} 行 × {len(table.columns)} 列"
    if not getattr(shape, "has_text_frame", False) or not shape.text_frame.text.strip():
        return f"{kind} [{box}]"
    text = " / ".join(part.strip() for part in shape.text_frame.text.splitlines() if part.strip())
    if len(text) > 60:
        text = text[:60] + "…"
    # 字号要给全：只报最大值时，模型看不出"哪几处小于 10pt"，只能再写脚本 dump 一遍
    sizes = [
        run.font.size.pt
        for paragraph in shape.text_frame.paragraphs
        for run in paragraph.runs
        if run.font.size
    ]
    if sizes:
        low, high = min(sizes), max(sizes)
        size = f" {low:g}pt" if low == high else f" {low:g}~{high:g}pt"
    else:
        size = " 字号继承主题"
    return f"{kind} [{box}]{size} 「{text}」"
    """模板清单：每页对象数与可替换槽位（位置 · 字号 · 字数上限 · 原文）。"""
    if not source.is_file():
        raise RuntimeError(f"模板不存在：{source}")
    if source.suffix.lower() != ".pptx":
        raise RuntimeError(f"模板需要是 .pptx（当前：{source.name}）")
    book = Presentation(str(source))
    lines = [
        f"模板：{source}",
        f"共 {len(book.slides)} 页，画布 {Emu(book.slide_width).cm:.1f}×{Emu(book.slide_height).cm:.1f} 厘米"
        "（16:9）",
    ]
    for index, slide in enumerate(book.slides, start=1):
        pictures = sum(1 for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE)
        head = f"第 {index} 页：{len(slide.shapes)} 个对象"
        if pictures:
            head += f"、图片 {pictures} 张"
        lines.append(head)
        grouped: dict[tuple, dict] = {}
        for row in slot_rows(slide, book.slide_width, book.slide_height):
            # 同位置同字号同原文的算同一个槽位，合并计数（模板常放 6 个相同占位）
            key = (row["pos"], row["size"], row["width"], row["text"])
            if key in grouped:
                grouped[key]["count"] += 1
                continue
            grouped[key] = {**row, "count": 1}
        for item in list(grouped.values())[:ANCHOR_LIMIT]:
            size = f"{item['size']:.0f}pt" if item["size"] else "字号继承"
            width = f"{item['width']:.0f}cm" if item["width"] else "宽?"
            chars = f"每行≈{item['per_line']}字" if item["per_line"] else ""
            times = f" ×{item['count']}" if item["count"] > 1 else ""
            text = item["text"]
            shown = text if len(text) <= ANCHOR_SHOWN else text[:ANCHOR_SHOWN] + "…"
            lines.append(f"    {item['pos']}　{size}　{width} {chars}　{shown}{times}")
        if len(grouped) > ANCHOR_LIMIT:
            lines.append(f"    …（另有 {len(grouped) - ANCHOR_LIMIT} 类槽位）")
    lines.append(
        "套版：slides=[{template_page, replace, drop, images, notes}]。replace 的键取上面的槽位原文"
        "（按段内匹配，长占位用前十几个字即可），命中段落整段换成新文本；"
        "带 ×N 的槽位把新文本写成数组、按上面列出的顺序依次填（同位置同字号的 N 个槽位）。"
        "按「每行≈N字」控制，一般写 2-3 行；超出就拆页或换用别的模板页。槽位里带示例数据与"
        "「请在此添加」「您的内容打在这里」这类占位的，换成真实内容或用 drop 清掉"
    )
    return "\n".join(lines)


def template_report(
    target: Path,
    written: list[dict],
    total: int,
    append: bool,
    note: str,
    source: Path,
    template_book,
) -> str:
    """套模板模式的回执：每页用的模板页、换了多少段、删了多少占位、贴了几张图。"""
    lines = [
        f"已{'追加' if append else '创建'} {target}{note}",
        f"版式来源：{source.name}（{len(template_book.slides)} 页模板）",
        f"页数：本次套版 {len(written)} 页，文件共 {total} 页",
    ]
    for item in written:
        pieces = [f"第 {item['index']} 页 ← 模板第 {item['page_no']} 页"]
        if item["texts"]:
            pieces.append(f"共 {item['texts']} 段文本")
        if item["changed"]:
            pieces.append(f"换文案 {item['changed']} 段")
        if item["dropped"]:
            pieces.append(f"清占位 {item['dropped']} 个")
        if item["pictures"]:
            pieces.append(f"贴图 {len(item['pictures'])} 张")
        if item["notes"]:
            pieces.append(f"备注 {len(item['notes'])} 字")
        lines.append("　".join(pieces))
    return "\n".join(lines)


#: pptx 版式（页型、色块）依赖这些颜色键，缺一个都画不出来
REQUIRED_COLORS = ("primary", "secondary", "light", "accent", "white")


def merge_theme(theme: Optional[dict]) -> dict:
    """把调用方给的 theme 规范成 ``{font, colors, body_sizes}`` —— **工具不带任何默认样式**。

    配色、字体、字号档位都由 ``theme`` 给，参数以对应**技能**里的 theme 段为准（技能是版式的
    唯一权威来源）。pptx 的页型实现离不开这五个颜色键，所以缺了直接报错——**不猜、也不内置
    一套默认配色**，否则"工具带风格"和"参数在技能里"这两条就都破功了。
    """
    if not isinstance(theme, dict):
        raise RuntimeError(
            "pptx 需要 theme：配色与字号档位由技能给出（读对应技能的 theme 段，原样传进来）"
        )
    # font 可以是字符串（中英同字体）或 {latin, east_asian}（分别设置）
    raw_font = theme.get("font")
    if isinstance(raw_font, dict):
        latin = str(raw_font.get("latin") or raw_font.get("western") or "").strip()
        east = str(raw_font.get("east_asian") or raw_font.get("chinese") or "").strip() or latin
    else:
        latin = str(raw_font or "").strip()
        east = str(theme.get("font_east_asian") or "").strip() or latin
    colors = {str(name): color(value) for name, value in (theme.get("colors") or {}).items()}
    missing = [name for name in REQUIRED_COLORS if name not in colors]
    if missing:
        raise RuntimeError(
            f"pptx 的 theme.colors 缺少 {'、'.join(missing)}：按技能里的 theme 段补齐"
            f"（需要 {'、'.join(REQUIRED_COLORS)}）"
        )
    return {
        # 中英文字体分开：中文走 a:ea，西文走 a:latin。
        # 只给一个字符串时两者相同（老用法）；给 {latin, east_asian} 就分别设置
        "latin": latin,
        "east_asian": east,
        "colors": colors,
        "body_sizes": [list(pair) for pair in (theme.get("body_sizes") or [])],
    }


def color(value) -> RGBColor:
    """接受 "#2B2D42" 或 "2B2D42"（也接受已是 RGBColor 的值）。"""
    if isinstance(value, RGBColor):
        return value
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        raise RuntimeError(f"颜色需要 6 位十六进制（如 #2B2D42）：{value!r}")
    try:
        return RGBColor.from_string(text.upper())
    except ValueError as error:
        raise RuntimeError(f"颜色无效：{value!r}（{error}）") from None


def add_slide(presentation, index: int, page, total: int, spec: dict, base_dir=None) -> None:
    """按页型绘制一页。"""
    child = page if isinstance(page, dict) else {"title": str(page)}
    bullets = [str(item).strip() for item in (child.get("bullets") or []) if str(item).strip()]
    kind = str(child.get("type") or ("cover" if index == 1 and not bullets else "content")).lower()
    title = str(child.get("title") or "").strip()
    subtitle = str(child.get("subtitle") or "").strip()
    colors = spec["colors"]
    slide = presentation.slides.add_slide(blank_layout(presentation))

    if kind == "cover":
        set_background(slide, colors["primary"])
        if title:
            add_textbox(slide, title, left=1.1, top=2.6, width=11.1, height=1.8,
                         size=56, color=colors["white"], bold=True, spec=spec)
        if subtitle:
            add_textbox(slide, subtitle, left=1.1, top=4.5, width=11.1, height=1.0,
                         size=24, color=colors["light"], valign="top", spec=spec)
        add_block(slide, left=0, top=7.05, width=SLIDE_WIDTH, height=0.45, color=colors["accent"])
    elif kind == "section":
        set_background(slide, colors["light"])
        add_block(slide, left=0, top=0, width=0.6, height=SLIDE_HEIGHT, color=colors["primary"])
        if title:
            add_textbox(slide, title, left=1.6, top=2.7, width=10.5, height=1.6,
                         size=48, color=colors["primary"], bold=True, spec=spec)
        if subtitle:
            add_textbox(slide, subtitle, left=1.6, top=4.4, width=10.5, height=1.0,
                         size=22, color=colors["secondary"], valign="top", spec=spec)
    elif kind == "two_column":
        set_background(slide, colors["white"])
        add_block(slide, left=0, top=0, width=SLIDE_WIDTH, height=1.45, color=colors["primary"])
        if title:
            add_textbox(slide, title, left=0.8, top=0.3, width=11.7, height=0.85,
                         size=36, color=colors["white"], bold=True, spec=spec)
        for position, key in enumerate(("left", "right")):
            column = child.get(key) or {}
            if not isinstance(column, dict):
                raise RuntimeError(f"第 {index} 页的 {key} 需要是 {{title, bullets}}")
            left = 0.9 + position * 5.9
            add_block(slide, left=left - 0.15, top=1.95, width=0.06, height=4.4, color=colors["accent"])
            column_title = str(column.get("title") or "").strip()
            if column_title:
                add_textbox(slide, column_title, left=left, top=1.95, width=5.4, height=0.6,
                             size=24, color=colors["primary"], bold=True, spec=spec)
            items = [str(item).strip() for item in (column.get("bullets") or []) if str(item).strip()]
            if items:
                add_textbox(slide, "\n".join(f"· {item}" for item in items),
                             left=left, top=2.7, width=5.4, height=3.6,
                             size=body_size(len(items), spec), color=colors["primary"],
                             line_spacing=1.35, spec=spec)
        add_block(slide, left=0, top=7.33, width=SLIDE_WIDTH, height=0.17, color=colors["secondary"])
        if total:
            add_textbox(slide, f"{index} / {total}", left=12.0, top=6.9, width=1.0, height=0.4,
                         size=14, color=colors["secondary"], align="right", spec=spec)
    elif kind == "content":
        set_background(slide, colors["white"])
        add_block(slide, left=0, top=0, width=SLIDE_WIDTH, height=1.45, color=colors["primary"])
        if title:
            add_textbox(slide, title, left=0.8, top=0.3, width=11.7, height=0.85,
                         size=36, color=colors["white"], bold=True, spec=spec)
        image = child.get("image")
        body_top, body_height, body_width = 2.0, 4.3, 11.2
        if isinstance(image, dict) and image.get("path"):
            placed = add_picture(slide, image, base_dir)
            body_top, body_height, body_width = 1.6, 1.5, 11.2
            spec.setdefault("_pictures", []).append(placed)
        if bullets:
            add_textbox(slide, "\n".join(f"· {bullet}" for bullet in bullets),
                         left=1.05, top=body_top, width=body_width, height=body_height,
                         size=body_size(len(bullets), spec), color=colors["primary"],
                         line_spacing=1.4, spec=spec)
        add_block(slide, left=0, top=7.33, width=SLIDE_WIDTH, height=0.17, color=colors["secondary"])
        if total:
            add_textbox(slide, f"{index} / {total}", left=12.0, top=6.9, width=1.0, height=0.4,
                         size=14, color=colors["secondary"], align="right", spec=spec)
    else:
        raise RuntimeError(f"不支持的页型：{kind}（可用 cover/section/content/two_column）")

    notes = str(child.get("notes") or "").strip()
    if notes:
        slide.notes_slide.notes_text_frame.text = notes


def add_picture(slide, image: dict, base_dir=None) -> str:
    """在内容页插图（图片放在标题条下方居中）。"""
    path = str(image.get("path") or "").strip()
    source = resolve(path, base_dir)
    if not source.is_file():
        raise RuntimeError(f"图片不存在：{path}")
    width = image.get("width_cm")
    target_width = Inches(float(width) / 2.54) if width else None
    if target_width:
        slide.shapes.add_picture(str(source), Inches(1.05), Inches(1.6), width=target_width)
    else:
        slide.shapes.add_picture(str(source), Inches(1.05), Inches(1.6))
    return source.name + (f"（宽 {float(width):g} 厘米）" if width else "")


def blank_layout(presentation):
    """取空白版式（按名称匹配，回退到第 7 个版式）。"""
    for layout in presentation.slide_layouts:
        if layout.name in ("Blank", "空白"):
            return layout
    return presentation.slide_layouts[6]


def set_background(slide, color: RGBColor) -> None:
    """设置页面纯色背景。"""
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def add_block(slide, *, left: float, top: float, width: float, height: float, color: RGBColor):
    """添加无边框纯色块（装饰条）。"""
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def add_textbox(
    slide,
    text: str,
    *,
    left: float,
    top: float,
    width: float,
    height: float,
    size: int,
    color: RGBColor,
    spec: dict,
    bold: bool = False,
    align: str = "left",
    valign: str = "middle",
    line_spacing: float = None,
) -> None:
    """添加文本框；多行文本逐段写入并套用主题字体。"""
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE if valign == "middle" else MSO_ANCHOR.TOP
    alignments = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
    for position, line in enumerate(text.split("\n")):
        paragraph = frame.paragraphs[0] if position == 0 else frame.add_paragraph()
        paragraph.alignment = alignments.get(align, PP_ALIGN.LEFT)
        if line_spacing:
            paragraph.line_spacing = line_spacing
        run = paragraph.add_run()
        run.text = line
        if size:
            run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        latin_font = str(spec.get("latin") or "").strip()
        east_font = str(spec.get("east_asian") or "").strip()
        if latin_font:
            run.font.name = latin_font
        if east_font:
            apply_east_asian_font(run, east_font)


def apply_east_asian_font(run, font_name: str) -> None:
    """设置东亚字体（PowerPoint 中文字体走 a:ea）。"""
    properties = run.font._rPr
    element = properties.find(qn("a:ea"))
    if element is None:
        element = properties.makeelement(qn("a:ea"), {})
        properties.append(element)
    element.set("typeface", font_name)


def body_size(count: int, spec: dict) -> int:
    """要点数量 → 正文字号（档位由 theme 给）；没给档位就返回 0，交给版式默认。"""
    sizes = spec.get("body_sizes") or []
    for limit, size in sizes:
        if count <= int(limit):
            return int(size)
    return int(sizes[-1][1]) if sizes else 0


def report(target: Path, slides: list, total: int, append: bool, note: str, spec: dict) -> str:
    """落盘回执：每页类型、标题、要点条数、图片与备注情况。"""
    presentation = Presentation(str(target))
    lines = [
        f"已{'追加' if append else '创建'} {target}{note}",
        f"页数：本次写入 {len(slides)} 页，文件共 {total} 页",
    ]
    for index, page in enumerate(slides, start=1):
        child = page if isinstance(page, dict) else {"title": str(page)}
        title = str(child.get("title") or "").strip()
        kind = str(child.get("type") or ("cover" if index == 1 else "content")).lower()
        bullets = [str(item).strip() for item in (child.get("bullets") or []) if str(item).strip()]
        pieces = [f"第 {index} 页：「{title}」" if title else f"第 {index} 页：（无标题）", f"类型={TYPE_NAMES.get(kind, kind)}"]
        if kind == "two_column":
            left = child.get("left") or {}
            right = child.get("right") or {}
            pieces.append(
                f"左栏 {len(left.get('bullets') or []) if isinstance(left, dict) else 0} 条"
                f" / 右栏 {len(right.get('bullets') or []) if isinstance(right, dict) else 0} 条"
            )
        elif bullets:
            pieces.append(f"要点 {len(bullets)} 条")
        if isinstance(child.get("image"), dict):
            pieces.append("含图片")
        if str(child.get("notes") or "").strip():
            pieces.append(f"备注 {len(str(child['notes']))} 字")
        lines.append("　".join(pieces))
    if spec.get("latin") or spec.get("east_asian"):
        font_note = f"字体：中文 {spec.get('east_asian') or '未指定'} / 西文 {spec.get('latin') or '未指定'}"
    else:
        font_note = "字体：未指定（用版式默认）"
    lines.append(f"{font_note}，共 {len(presentation.slides)} 页可正常打开")
    return "\n".join(lines)


def run_script(target: Path, script: str) -> str:
    """在已存在的演示文稿上执行 python-pptx 代码，保存并回报结构变化。"""
    if not target.is_file():
        raise RuntimeError(f"文件不存在：{target.name}（先用 template 或 slides 生成，再用 script 微调）")
    presentation = Presentation(str(target))

    def snapshot() -> dict:
        return {
            "页数": len(presentation.slides),
            "各页对象数": [len(slide.shapes) for slide in presentation.slides],
        }

    before, after, output = docscript.run(
        script,
        {
            "prs": presentation,
            "slides": presentation.slides,
            "Cm": Cm,
            "Emu": Emu,
            "Inches": Inches,
            "Pt": Pt,
            "RGBColor": RGBColor,
            "MSO_SHAPE": MSO_SHAPE,
            "MSO_SHAPE_TYPE": MSO_SHAPE_TYPE,
            "PP_ALIGN": PP_ALIGN,
            "MSO_ANCHOR": MSO_ANCHOR,
            "qn": qn,
        },
        lambda: presentation.save(str(target)),
        snapshot,
    )
    lines = [f"已执行脚本（{target.name}）", f"结构：{docscript.diff(before, after)}"]
    if output.strip():
        lines.append("脚本输出：")
        lines.extend(f"  {line}" for line in output.strip().splitlines()[:20])
    return "\n".join(lines)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def avoid_overwrite(target: Path) -> tuple[Path, bool]:
    """同名文件已存在时另存为 ``name-1.pptx``、``name-2.pptx``…，返回 (实际路径, 是否改名)。"""
    if not target.exists():
        return target, False
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate, True
    raise RuntimeError(f"同名文件过多，请换一个文件名：{target.name}")
