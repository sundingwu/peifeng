"""本技能的格式校验与原子保存。"""
import zipfile
from pathlib import Path
from backend.utils.skill_runtime import atomic_output, _UNSET

def validate_office(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() or len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('Office包校验失败')
    if path.suffix.lower() in {'.xlsx', '.xlsm'}:
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, keep_vba=path.suffix.lower() == '.xlsm')
        try:
            for sheet in workbook:
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.data_type == 'e':
                            raise ValueError(f'工作簿含错误单元格：{sheet.title}!{cell.coordinate}={cell.value}')
        finally:
            workbook.close()

def save_office(document, target: Path, expected=_UNSET, *, sources=()) -> None:
    with atomic_output(target, expected=expected, sources=sources) as candidate:
        document.save(candidate)
        validate_office(candidate)
