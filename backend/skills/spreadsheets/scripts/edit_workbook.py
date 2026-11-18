"""文档技能的Python执行入口；不注册为agent tool。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workbook_files import save_office, validate_office
from copy import deepcopy
from types import SimpleNamespace
from backend.utils.web import run_targets
from backend.utils.skill_runtime import project_path, file_digest

def build_xlsx():
    """工作簿制作脚本：创建或覆盖 Excel 工作簿，支持多工作表、数字格式、合并与冻结。

工具只管"怎么落进表格"，不预设任何报表风格：

- ``sheets`` 声明每张工作表：名称、表头、数据行、列宽、数字格式、合并区域、冻结位置
- ``theme`` 声明风格：字体字号、表头加粗与填充色、边框、对齐；**不给就不设字体**（工具不带默认样式），参数由技能给出

报表风格（成绩单、汇总表、台账等）由技能定义，技能提供 ``theme`` 与列格式约定。

脚本成功返回实际产物回执，失败抛出异常。
"""

    from typing import Optional

    from backend.utils.web import run_targets

    #: 一次最多处理几个目标


    from pathlib import Path
    from typing import Any

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter


    #: 列宽范围（字符数）
    MIN_WIDTH = 8
    MAX_WIDTH = 60

    #: 对齐取值 → openpyxl 的 horizontal
    ALIGNS = ("left", "center", "right")




    def xlsx_target(
        file: str = '',
        sheets: Optional[list] = None,
        theme: Optional[dict] = None,
        style: Optional[dict] = None,
        overwrite: bool = False,
        script: str = '',
        base_dir=None
    ) -> str:
        """写入、覆盖或跑脚本改 Excel 工作簿，返回落盘回执；相对路径以 base_dir 为基准。"""
        if not str(file or "").strip():
            raise RuntimeError("xlsx 需要 file 参数")
        if script:
            raise RuntimeError("script 参数已停用：系统不执行任意代码，请改用结构化参数")
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
            if target.exists():
                raise ValueError('输出已存在；覆盖需overwrite=true')
            renamed = False
            replaced = False

        expected_digest = file_digest(target)
        workbook = Workbook()
        workbook.remove(workbook.active)
        written: list[dict] = []
        for position, sheet_spec in enumerate(sheets, start=1):
            written.append(write_sheet(workbook, sheet_spec, spec, position))
        save_office(workbook, target, expected_digest)

        if replaced:
            note = "（已覆盖同名文件）"
        else:
            note = f"（同名文件已存在，为避免覆盖另存为 {target.name}）" if renamed else ""
        return report(target, written, note)


    def write_sheet(workbook, sheet_spec: dict, spec: dict, position: int) -> dict:
        """写入一张工作表，返回它的结构记录（供回执）。"""
        if not isinstance(sheet_spec, dict):
            raise RuntimeError(f"sheets[{position - 1}] 需要是对象：{{name, header, rows}}")
        name = str(sheet_spec.get("name") or f"Sheet{position}").strip()
        if len(name) > 31 or any(char in name for char in "[]:*?/\\"):
            raise ValueError("工作表名称超过31字符或含非法字符，不能静默改名")
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
        return project_path(path, base_dir)


    def avoid_overwrite(target: Path) -> tuple[Path, bool]:
        """同名文件已存在时另存为 ``name-1.xlsx``、``name-2.xlsx``…，返回 (实际路径, 是否改名)。"""
        if not target.exists():
            return target, False
        for index in range(1, 1000):
            candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
            if not candidate.exists():
                return candidate, True
        raise RuntimeError(f"同名文件过多，请换一个文件名：{target.name}")


    def xlsx(targets: list[dict], base_dir=None) -> str:
        """统一批处理入口，每项独立参数。"""
        return run_targets(lambda target: xlsx_target(**target, base_dir=base_dir), targets, workers=8, base_dir=base_dir)
    return xlsx

xlsx = build_xlsx()


FORMULA_ERRORS = {'#REF!', '#DIV/0!', '#VALUE!', '#N/A', '#NAME?', '#NUM!', '#NULL!', '#SPILL!', '#CALC!'}


def inspect_workbook(path: Path) -> dict:
    from openpyxl import load_workbook
    formulas = load_workbook(path, data_only=False, read_only=True)
    values = load_workbook(path, data_only=True, read_only=True)
    errors, not_computed, formula_count, sheets = [], [], 0, []
    try:
        for sheet in formulas:
            cached = values[sheet.title]
            sheets.append({'name':sheet.title,'rows':sheet.max_row,'columns':sheet.max_column})
            for row in sheet.iter_rows():
                for cell in row:
                    value = cell.value
                    if cell.data_type == 'e':
                        errors.append({'cell':f'{sheet.title}!{cell.coordinate}','error':value})
                    if cell.data_type == 'f':
                        formula_count += 1
                        result = cached[cell.coordinate].value
                        if cached[cell.coordinate].data_type == 'e':
                            errors.append({'cell':f'{sheet.title}!{cell.coordinate}','error':result})
                        elif result is None:
                            not_computed.append(f'{sheet.title}!{cell.coordinate}')
        return {'file':str(path),'files':[],'sheets':sheets,'formula_count':formula_count,
                'errors':errors,'not_computed':not_computed,'not_computed_count':len(not_computed),
                'status':'fail' if errors else 'not_computed' if not_computed else 'pass',
                'scope':'检查公式字符串与已有缓存错误；未执行Excel公式重算；缓存为空也可能是空字符串结果。'}
    finally:
        formulas.close(); values.close()


def check_editable_source(path: Path) -> None:
    import zipfile
    import defusedxml.ElementTree as ET
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith(('xl/slicers/', 'xl/timelines/', 'xl/activeX/')):
                raise ValueError('工作簿含切片器、时间线或控件；openpyxl保存可能丢失，需定点编辑XML')
            if name.startswith('xl/drawings/') and name.endswith('.xml'):
                if any(node.tag.endswith('}sp') or node.tag.endswith('}cxnSp') for node in ET.fromstring(archive.read(name)).iter()):
                    raise ValueError('工作簿含原生形状或文本框；请定点编辑XML，不能用openpyxl整体保存')


def process(target: dict, base: Path) -> dict:
    action = target.pop('action', 'create')
    if action in {'inspect','check'}:
        result=inspect_workbook(project_path(target.get('path') or target.get('file'),base))
        if action=='check' and result['errors']:
            raise ValueError('工作簿包含公式错误：'+str(result['errors'][:20]))
        return result
    if action=='recalculate':
        raise ValueError('纯Python路径不提供通用Excel公式重算；不能把未计算缓存报告为通过')
    if action=='create':
        log=xlsx([target],base)
        output=project_path(target['file'],base)
        return {**inspect_workbook(output),'files':[str(output)],'log':log}
    if action!='edit':
        raise ValueError('action支持create/edit/inspect/check')
    from openpyxl import load_workbook
    source=project_path(target['path'],base)
    output=project_path(target.get('file',target['path']),base)
    if source.suffix.lower() not in {'.xlsx','.xlsm'} or output.suffix.lower()!=source.suffix.lower():
        raise ValueError('编辑时保持.xlsx或.xlsm后缀；不静默丢弃宏')
    if output.exists() and not target.get('overwrite',False):
        raise ValueError('输出已存在；覆盖需overwrite=true')
    check_editable_source(source)
    source_digest=file_digest(source); expected=file_digest(output)
    book=load_workbook(source,data_only=False,keep_vba=source.suffix.lower()=='.xlsm',rich_text=True)
    try:
        patches=target.get('patch')
        if not isinstance(patches,list) or not patches:
            raise ValueError('edit需要非空patch数组，含sheet/cell/value')
        for patch in patches:
            cell=book[patch['sheet']][patch['cell']]
            cell.value=patch['value']
            if patch.get('number_format') is not None:
                cell.number_format=patch['number_format']
        save_office(book,output,expected,sources=[(source,source_digest)])
    finally:
        book.close()
    return {**inspect_workbook(output),'files':[str(output)]}


if __name__ == '__main__':
    from backend.utils.skill_runtime import run_cli
    run_cli(process)
