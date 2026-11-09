"""xlsx 工具（写）：创建或覆盖 Excel 工作簿，支持多工作表、数字格式、合并与冻结。

工具只管"怎么落进表格"，不预设任何报表风格：

- ``sheets`` 声明每张工作表：名称、表头、数据行、列宽、数字格式、合并区域、冻结位置
- ``theme`` 声明风格：字体字号、表头加粗与填充色、边框、对齐；**不给就不设字体**（工具不带默认样式），参数由技能给出

报表风格（成绩单、汇总表、台账等）由技能定义，技能提供 ``theme`` 与列格式约定。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from peifeng_agent.backend.utils import docscript

#: 列宽范围（字符数）
MIN_WIDTH = 8
MAX_WIDTH = 60

#: 对齐取值 → openpyxl 的 horizontal
ALIGNS = ("left", "center", "right")

information = {
    "name": "xlsx",
    "description": (
        "操作 Excel 工作簿（.xlsx）：sheets 写工作表，overwrite 覆盖重写。"
        "用于：生成成绩单、统计表、台账等 .xlsx；组织多张表（明细加汇总）；套用数字格式与冻结首行时。"
        "sheets 每项：{name, header, rows, widths, number_formats, merge, freeze}——"
        "header 表头（加粗）、rows 二维数据行、widths 列宽（{\"A\": 12} 或数组，字符数）、"
        "number_formats 数字格式（{\"D\": \"#,##0.00\", \"E\": \"0.0%\"}）、"
        "merge 合并区域（[\"A1:C1\"]）、freeze 冻结位置（\"A2\" 冻结首行）。"
        "theme 声明风格：{font, size, header_bold, header_fill(十六进制色), border, align}；"
        "**工具本身不带默认字体字号**：theme 不给就不设字体（由 Excel 默认决定），参数从技能里取。"
        "返回结构回执：每表列数行数、表头、列宽、数字格式"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "目标文件（.xlsx，放 <工作目录> 下）"},
            "sheets": {
                "type": "array",
                "description": "工作表列表，每项 {name, header, rows, widths, number_formats, merge, freeze}",
                "items": {},
            },
            "theme": {
                "type": "object",
                "description": "风格定义（字体字号、表头填充、边框、对齐）；不给就不设字体，参数从技能取",
            },
            "style": {
                "type": "object",
                "description": (
                    "文档格式（与 theme 同义，优先用这个）：字体、字号、配色、页边距、行距等；参数以技能的 style/theme 段为准——工具本身不带任何默认样式"
                ),
            },
            "overwrite": {"type": "boolean", "description": "true 时覆盖同名文件重写（改表用）"},
            "script": {
                "type": "string",
                "description": "对已生成的文件跑一段 openpyxl 代码（透传库的全部能力）：变量 wb 是 Workbook，"
                "sheet(name) 取工作表，另有 Font/Alignment/Border/Side/PatternFill、get_column_letter。"
                "用于条件格式、数据透视、图表、合并单元格、逐格改样式等 sheets 表达不了的操作",
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


def xlsx(
    file: str = "",
    sheets: Optional[list] = None,
    theme: Optional[dict] = None,
    style: Optional[dict] = None,
    overwrite: bool = False,
    script: str = "",
    documents: list = None,
    base_dir=None,
) -> str:
    """写入、覆盖或跑脚本改 Excel 工作簿，返回落盘回执；相对路径以 base_dir 为基准。"""
    batch = as_dict_list(documents, "documents", MAX_BATCH)
    if batch:
        return call_many(xlsx, batch, "documents", base_dir=base_dir, preview="file")
    if not str(file or "").strip():
        raise RuntimeError("xlsx 需要 file（单个）或 documents（多个）之一")
    if script:
        return run_script(resolve(file, base_dir), script)
    target = resolve(file, base_dir)
    if target.suffix.lower() != ".xlsx":
        raise RuntimeError(f"xlsx 只支持 .xlsx（当前：{file}）")
    if not sheets:
        raise RuntimeError("没有可写入的内容：请给 sheets（[{name, header, rows}]）")

    spec = merge_theme(style or theme)
    target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite and target.exists():
        replaced, renamed = True, False
    else:
        target, renamed = avoid_overwrite(target)
        replaced = False

    workbook = Workbook()
    workbook.remove(workbook.active)
    written: list[dict] = []
    for position, sheet_spec in enumerate(sheets, start=1):
        written.append(write_sheet(workbook, sheet_spec, spec, position))
    workbook.save(str(target))

    if replaced:
        note = "（已覆盖同名文件）"
    else:
        note = f"（同名文件已存在，为避免覆盖另存为 {target.name}）" if renamed else ""
    return report(target, written, note)


def write_sheet(workbook, sheet_spec: dict, spec: dict, position: int) -> dict:
    """写入一张工作表，返回它的结构记录（供回执）。"""
    if not isinstance(sheet_spec, dict):
        raise RuntimeError(f"sheets[{position - 1}] 需要是对象：{{name, header, rows}}")
    name = str(sheet_spec.get("name") or f"Sheet{position}").strip()[:31]
    if name in workbook.sheetnames:
        raise RuntimeError(f"工作表名重复：{name}")
    header = [str(item) for item in (sheet_spec.get("header") or [])]
    rows = sheet_spec.get("rows") or []
    if not header and not rows:
        raise RuntimeError(f"工作表 {name} 没有内容：需要 header 或 rows")

    sheet = workbook.create_sheet(title=name)
    columns = max((len(header), *(len(row) for row in rows)), default=0)
    if not columns:
        raise RuntimeError(f"工作表 {name} 的 header 与 rows 都是空的")

    line = 1
    if header:
        for column, value in enumerate(header, start=1):
            cell = sheet.cell(row=1, column=column, value=value)
            style_cell(cell, spec, header=True)
        line = 2
    for offset, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise RuntimeError(f"工作表 {name} 的第 {offset + 1} 行需要是数组")
        for column in range(columns):
            value = row[column] if column < len(row) else ""
            style_cell(sheet.cell(row=line + offset, column=column + 1, value=value), spec, header=False)

    # 数字格式：按列字母套用整列（含表头以下的数据区）
    row_count = line - 1 + len(rows)
    for letter, pattern in (sheet_spec.get("number_formats") or {}).items():
        index = column_index(str(letter))
        for row_number in range(2 if header else 1, row_count + 1):
            sheet[f"{letter}{row_number}"].number_format = str(pattern)
        sheet.column_dimensions[get_column_letter(index)].number_format = str(pattern)

    for area in sheet_spec.get("merge") or []:
        try:
            sheet.merge_cells(str(area))
        except (ValueError, KeyError) as error:
            raise RuntimeError(f"工作表 {name} 的合并区域无效：{area}（{error}）") from None

    freeze = str(sheet_spec.get("freeze") or "").strip()
    if freeze:
        sheet.freeze_panes = freeze

    apply_widths(sheet, sheet_spec.get("widths"), columns)
    return {
        "name": name,
        "columns": columns,
        "rows": len(rows),
        "header": header,
        "formats": dict(sheet_spec.get("number_formats") or {}),
        "widths": {
            get_column_letter(index): sheet.column_dimensions[get_column_letter(index)].width
            for index in range(1, columns + 1)
        },
    }


def column_index(letter: str) -> int:
    """列字母 → 列号（A → 1）。"""
    index = 0
    for char in letter.strip().upper():
        if not char.isalpha():
            break
        index = index * 26 + (ord(char) - 64)
    return index or 1


def merge_theme(theme: Optional[dict]) -> dict:
    """把调用方给的 theme 规范成样式字典 —— **不填任何默认字体字号**。

    工具本身不带风格：想有版式就把 ``theme`` 传进来，参数以对应**技能**里的 theme 段为准。
    """
    spec: dict[str, Any] = {}
    if isinstance(theme, dict):
        spec = {str(key): value for key, value in theme.items()}
    align = str(spec.get("align") or "").strip().lower()
    spec["align"] = align if align in ALIGNS else ""
    return spec


def style_cell(cell, spec: dict, *, header: bool) -> None:
    """套用字体、填充、边框与对齐；theme 里没给的项目一律不动（由 Excel 默认决定）。"""
    bold = bool(spec.get("header_bold", True)) if header else False
    font_name = str(spec.get("font") or "").strip()
    size = spec.get("size")
    cell.font = Font(
        name=font_name or None,
        size=float(size) if size else None,
        bold=bold,
    )
    align = str(spec.get("align") or "").strip()
    cell.alignment = Alignment(horizontal=align or None, vertical="center")
    fill = str(spec.get("header_fill") or "").strip()
    if header and fill:
        cell.fill = PatternFill("solid", start_color=fill.lstrip("#").upper())
    if spec.get("border"):
        side = Side(style="thin", color="B4C6E7")
        cell.border = Border(left=side, right=side, top=side, bottom=side)


def apply_widths(sheet, widths, columns: int) -> None:
    """设置列宽：支持 {"A": 12} 或 [12, 20, 8]；没给的列按内容估算。"""
    given: dict[str, float] = {}
    if isinstance(widths, dict):
        given = {str(letter).upper(): float(value) for letter, value in widths.items()}
    elif isinstance(widths, (list, tuple)):
        given = {get_column_letter(index): float(value) for index, value in enumerate(widths, start=1)}
    for index in range(1, columns + 1):
        letter = get_column_letter(index)
        if letter in given:
            sheet.column_dimensions[letter].width = max(MIN_WIDTH, min(given[letter], MAX_WIDTH))
            continue
        longest = 0
        for cell in sheet[letter]:
            text = "" if cell.value is None else str(cell.value)
            length = sum(2 if ord(char) > 127 else 1 for char in text)
            longest = max(longest, length)
        sheet.column_dimensions[letter].width = max(MIN_WIDTH, min(longest + 2, MAX_WIDTH))


def run_script(target: Path, script: str) -> str:
    """在已存在的工作簿上执行 openpyxl 代码，保存并回报结构变化。"""
    if not target.is_file():
        raise RuntimeError(f"文件不存在：{target.name}（先用 sheets 生成，再用 script 微调）")
    from openpyxl import load_workbook

    workbook = load_workbook(str(target))

    def snapshot() -> dict:
        return {
            "工作表数": len(workbook.sheetnames),
            "各表行数": [sheet.max_row for sheet in workbook.worksheets],
        }

    before, after, output = docscript.run(
        script,
        {
            "wb": workbook,
            "sheet": workbook.__getitem__,
            "Font": Font,
            "Alignment": Alignment,
            "Border": Border,
            "Side": Side,
            "PatternFill": PatternFill,
            "get_column_letter": get_column_letter,
        },
        lambda: workbook.save(str(target)),
        snapshot,
    )
    lines = [f"已执行脚本（{target.name}）", f"结构：{docscript.diff(before, after)}"]
    if output.strip():
        lines.append("脚本输出：")
        lines.extend(f"  {line}" for line in output.strip().splitlines()[:20])
    return "\n".join(lines)


def report(target: Path, written: list[dict], note: str) -> str:
    """落盘回执：每张表的列数行数、表头、列宽与数字格式。"""
    lines = [f"已创建 {target}{note}", f"工作表 {len(written)} 张："]
    for item in written:
        lines.append(f"  {item['name']}：{item['columns']} 列 × {item['rows']} 个数据行")
        if item["header"]:
            lines.append(f"    表头（已加粗）：{'、'.join(item['header'])}")
        if item["widths"]:
            lines.append("    列宽（字符数）：" + "、".join(
                f"{letter}={width:.0f}" for letter, width in item["widths"].items() if width
            ))
        if item["formats"]:
            lines.append("    数字格式：" + "、".join(f"{letter}={pattern}" for letter, pattern in item["formats"].items()))
    return "\n".join(lines)


def resolve(path: str, base_dir) -> Path:
    """相对路径以 base_dir（项目工作文件夹）为基准；绝对路径原样使用。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def avoid_overwrite(target: Path) -> tuple[Path, bool]:
    """同名文件已存在时另存为 ``name-1.xlsx``、``name-2.xlsx``…，返回 (实际路径, 是否改名)。"""
    if not target.exists():
        return target, False
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate, True
    raise RuntimeError(f"同名文件过多，请换一个文件名：{target.name}")
