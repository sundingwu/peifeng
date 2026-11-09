"""离线保底执行：不调用任何 LLM，按固定流程把预设任务做完。

**为什么能离线**：预设任务的流程本来就是确定的——「读表格 → 按列画图」「按扩展名归档」
这类活的步骤不依赖模型决策。工具层（chart_kit / docx / pptx / xlsx）本身也不调模型，
所以只要绕过 planner 与 worker 的对话循环，直接调工具就能跑。

**代价**：需要"生成内容"的两个预设只能**重组素材原文**，不能总结改写——
``report_docx`` 与 ``pptx_deck`` 在离线模式下是"把材料重新排成文档/页面"，不是"写一篇新内容"。
输出里会明确标注这一点，避免用户误以为质量与在线模式相同。
"""
from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 离线结果：产出了什么、怎么做的、有哪些限制
@dataclass
class OfflineResult:
    preset: str
    files: list[str] = field(default_factory=list)
    summary: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 公共

def read_table(path: Path, sheet: str = "") -> list[list]:
    """把 .xlsx / .csv 读成二维列表（离线专用，不依赖 chart_kit 的路径解析）。"""
    from peifeng_agent.backend.utils.chart_kit import read_table as _read

    return _read(path, sheet)


def to_number(cell) -> Optional[float]:
    """单元格转数字；转不了返回 None（离线模式跳过而不是报错）。"""
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    text = str(cell).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def numeric_columns(rows: list[list]) -> tuple[list[str], list[int], list[str]]:
    """认出表头、数值列与分类列：数值列 = 前 20 行里超过一半能转成数字的列。"""
    header = [str(cell).strip() for cell in rows[0]]
    body = rows[1:]
    sample = body[:20]
    numeric: list[int] = []
    for index in range(len(header)):
        hits = sum(1 for row in sample if index < len(row) and to_number(row[index]) is not None)
        if sample and hits > len(sample) / 2:
            numeric.append(index)
    text_columns = [index for index in range(len(header)) if index not in numeric]
    return header, numeric, text_columns


# ---------------------------------------------------------------- 画图

CHARTS = {"柱状图": "bar", "条形图": "bar", "折线图": "line", "饼图": "pie", "散点图": "scatter"}


def run_chart(values: dict[str, str], workspace: Path) -> OfflineResult:
    """读表格 → 数值列全部画上 → 落盘 PNG。"""
    from peifeng_agent.backend.agent.executor.chart.tool.bar_chart import bar_chart
    from peifeng_agent.backend.agent.executor.chart.tool.line_chart import line_chart
    from peifeng_agent.backend.agent.executor.chart.tool.pie_chart import pie_chart

    source = (values.get("source") or "").strip()
    if not source:
        raise RuntimeError("chart_png 需要 source（数据文件）")
    path = (workspace / source) if not Path(source).is_absolute() else Path(source)
    rows = read_table(path)
    if len(rows) < 2:
        raise RuntimeError(f"{path.name} 只有表头没有数据")

    header, numeric, text_columns = numeric_columns(rows)
    if not numeric:
        raise RuntimeError(f"{path.name} 里没有能当数值的列（表头：{'、'.join(header)}）")
    if not text_columns:
        raise RuntimeError(f"{path.name} 里没有能当分类的列")

    category_index = text_columns[0]
    labels: list[str] = []
    buckets: list[list[float]] = [[] for _ in numeric]
    for row in rows[1:]:
        cell = row[category_index] if category_index < len(row) else None
        if cell is None or not str(cell).strip():
            continue
        labels.append(str(cell).strip())
        for position, index in enumerate(numeric):
            buckets[position].append(to_number(row[index] if index < len(row) else None) or 0.0)
    if not labels:
        raise RuntimeError("分类列全是空值，无法出图")

    series = [{"name": header[index], "values": bucket} for index, bucket in zip(numeric, buckets)]
    chart_type = CHARTS.get((values.get("chart") or "柱状图").strip(), "bar")
    metric = (values.get("metric") or "").strip() or "、".join(item["name"] for item in series)
    title = f"{path.stem}｜{metric}"
    output = f"{path.stem}_图表.png"

    notes = [f"离线模式：未理解「{metric}」，直接把所有数值列（{'、'.join(item['name'] for item in series)}）画进图里。"]
    if chart_type == "line":
        receipt = line_chart(output=output, title=title, xlabel=header[category_index],
                             ylabel="数值", categories=labels, series=series, base_dir=str(workspace))
    elif chart_type == "pie":
        if len(series) > 1:
            notes.append(f"饼图只画第一个数值列（{series[0]['name']}），其余列未进图。")
        receipt = pie_chart(output=output, title=title, categories=labels,
                            values=series[0]["values"], base_dir=str(workspace))
    else:
        receipt = bar_chart(output=output, title=title, xlabel=header[category_index],
                            ylabel="数值", categories=labels, series=series, base_dir=str(workspace))
    return OfflineResult(
        preset="chart_png",
        files=[str(workspace / output)],
        summary=f"已出图：{output}（{len(labels)} 个分类，{len(series)} 个系列）\n{receipt}",
        notes=notes,
    )


# ---------------------------------------------------------------- 表格整理

def run_table(values: dict[str, str], workspace: Path) -> OfflineResult:
    """读表格 → 去重去空 → 第一列分组、数值列求和 + 计数 → 写 .xlsx。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    source = (values.get("source") or "").strip()
    if not source:
        raise RuntimeError("table_xlsx 需要 source（数据文件）")
    path = (workspace / source) if not Path(source).is_absolute() else Path(source)
    rows = read_table(path)
    if len(rows) < 2:
        raise RuntimeError(f"{path.name} 只有表头没有数据")

    header = [str(cell).strip() for cell in rows[0]]
    body = rows[1:]
    seen: set[tuple] = set()
    cleaned: list[list] = []
    dropped_dup = dropped_empty = 0
    for row in body:
        if all(cell is None or not str(cell).strip() for cell in row):
            dropped_empty += 1
            continue
        key = tuple("" if cell is None else str(cell).strip() for cell in row)
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)
        cleaned.append(row)

    _, numeric, text_columns = numeric_columns(rows)
    group_index = text_columns[0] if text_columns else 0
    groups: dict[str, dict[str, float]] = {}
    for row in cleaned:
        key = str(row[group_index]).strip() if group_index < len(row) and row[group_index] is not None else "（空）"
        bucket = groups.setdefault(key, {"__count": 0.0, **{header[i]: 0.0 for i in numeric}})
        bucket["__count"] += 1
        for index in numeric:
            bucket[header[index]] += to_number(row[index] if index < len(row) else None) or 0.0

    output = (values.get("output") or "汇总表.xlsx").strip()
    target = (workspace / output) if not Path(output).is_absolute() else Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)

    book = Workbook()
    sheet = book.active
    sheet.title = "汇总"
    head = PatternFill("solid", fgColor="D9E2F3")
    sheet.append([header[group_index], "条数"] + [header[i] for i in numeric])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = head
    for key, bucket in sorted(groups.items(), key=lambda pair: -pair[1]["__count"]):
        sheet.append([key, int(bucket["__count"])] + [round(bucket[header[i]], 2) for i in numeric])

    detail = book.create_sheet("明细（清洗后）")
    detail.append(header)
    for row in cleaned:
        detail.append([None if cell is None else cell for cell in row])
    for column in sheet.columns:
        width = max(len(str(cell.value or "")) for cell in column)
        sheet.column_dimensions[column[0].column_letter].width = min(28, max(10, width * 1.6))
    book.save(target)

    note = (
        f"离线模式：按「第一列分组 + 数值列求和 + 计数」固定规则汇总，"
        f"没有理解「{values.get('summary') or '（未填）'}」这句话。"
    )
    return OfflineResult(
        preset="table_xlsx",
        files=[str(target)],
        summary=(
            f"已生成 {output}：清洗后 {len(cleaned)} 条（去重 {dropped_dup}、去空行 {dropped_empty}），"
            f"按「{header[group_index]}」分成 {len(groups)} 组"
        ),
        notes=[note],
    )


# ---------------------------------------------------------------- 归档

def run_archive(values: dict[str, str], workspace: Path) -> OfflineResult:
    """按扩展名把文件移进 归档/<ext>/ 并写清单。"""
    target_dir = (values.get("target") or "").strip()
    if not target_dir:
        raise RuntimeError("archive_files 需要 target（要整理的目录）")
    root = (workspace / target_dir) if not Path(target_dir).is_absolute() else Path(target_dir)
    if not root.is_dir():
        raise RuntimeError(f"目录不存在：{root}")

    archive = root / "归档"
    moved: list[tuple[str, str]] = []
    for item in sorted(root.iterdir()):
        if not item.is_file() or item.name == "归档清单.md":
            continue
        ext = item.suffix.lower().lstrip(".") or "无扩展名"
        folder = archive / ext
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / item.name
        if destination.exists():
            destination = folder / f"{item.stem}_1{item.suffix}"
        shutil.move(str(item), str(destination))
        moved.append((item.name, str(destination.relative_to(root))))

    manifest = archive / "归档清单.md"
    lines = ["# 归档清单", "", f"- 整理目录：{root.name}", f"- 共归档 {len(moved)} 个文件", ""]
    lines.append("| 原文件 | 归档到 |")
    lines.append("|---|---|")
    for name, destination in moved:
        lines.append(f"| {name} | {destination} |")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return OfflineResult(
        preset="archive_files",
        files=[str(manifest)],
        summary=f"已归档 {len(moved)} 个文件到 归档/ 下，清单：{manifest.name}",
        notes=[] if moved else ["目录里没有可归档的文件（已跳过子目录与清单本身）。"],
    )


# ---------------------------------------------------------------- 素材 → 文档（重组原文）

def material_text(path: Path) -> str:
    """读素材正文：md/txt/csv 直读，docx/xlsx/pptx 走 text 模块的解析。"""
    from peifeng_agent.backend.utils.text import content_lines

    return "\n".join(str(line) for line in content_lines(path))


def split_sections(text: str) -> list[tuple[str, list[str]]]:
    """按 Markdown 标题切节；没有标题就整篇算一节。"""
    sections: list[tuple[str, list[str]]] = []
    title = ""
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if title or body:
                sections.append((title, body))
            title = stripped.lstrip("#").strip()
            body = []
        elif stripped:
            body.append(stripped)
    if title or body:
        sections.append((title, body))
    return sections or [("", [line.strip() for line in text.splitlines() if line.strip()])]


def find_material(values: dict[str, str], workspace: Path, suffixes: tuple[str, ...]) -> Optional[Path]:
    """按表单给的素材名找文件；没给就在工作区里挑第一个可读的素材。"""
    name = (values.get("materials") or "").strip()
    if name and "未指定" not in name:
        path = (workspace / name) if not Path(name).is_absolute() else Path(name)
        if path.is_file():
            return path
    for item in sorted(workspace.iterdir()):
        if item.is_file() and item.suffix.lower() in suffixes:
            return item
    return None


def run_report(values: dict[str, str], workspace: Path) -> OfflineResult:
    """把素材按章节重新排成 .docx（原文重组，不总结）。"""
    from peifeng_agent.backend.agent.executor.office_docs.tool.docx import docx

    topic = (values.get("topic") or "汇报").strip()
    material = find_material(values, workspace, (".md", ".txt", ".docx"))
    if material is None:
        raise RuntimeError("找不到素材文件（materials 指定，或把素材放到项目目录）")
    sections = split_sections(material_text(material))

    blocks: list[dict[str, Any]] = [{"type": "title", "text": topic}]
    for title, body in sections:
        if title:
            blocks.append({"type": "heading", "level": 1, "text": title})
        for line in body[:60]:
            blocks.append({"type": "paragraph", "text": line})
    output = f"{topic}.docx"
    receipt = docx(file=output, blocks=blocks, overwrite=True, base_dir=str(workspace))

    return OfflineResult(
        preset="report_docx",
        files=[str(workspace / output)],
        summary=f"已生成 {output}（{len(sections)} 节，素材：{material.name}）\n{receipt.splitlines()[0]}",
        notes=[
            "离线模式：把素材**按章节重新排版**，没有做总结与改写。"
            f"篇幅要求（{values.get('length') or '未填'}）只能人工调整。",
        ],
    )


# ---------------------------------------------------------------- 素材 → PPT（重组原文）

def run_deck(values: dict[str, str], workspace: Path) -> OfflineResult:
    """把素材的每一节做成一页 PPT（标题 + 原文要点，不生成新内容）。"""
    from peifeng_agent.backend.agent.executor.office_docs.tool.pptx import pptx

    topic = (values.get("topic") or "汇报").strip()
    material = find_material(values, workspace, (".md", ".txt", ".docx", ".pptx"))
    if material is None:
        raise RuntimeError("找不到素材文件（materials 指定，或把素材放到项目目录）")
    sections = [item for item in split_sections(material_text(material)) if item[0] or item[1]]

    wanted = 5
    match = re.search(r"(\d+)\s*页", values.get("pages") or "")
    if match:
        wanted = max(2, min(30, int(match.group(1))))

    slides: list[dict[str, Any]] = [{"type": "cover", "title": topic, "subtitle": material.stem}]
    for title, body in sections[: max(1, wanted - 2)]:
        bullets = [line[:80] for line in body[:6]]
        slides.append({"type": "content", "title": title or topic, "bullets": bullets or ["（本节素材为空）"]})
    slides.append({"type": "section", "title": "谢谢"})

    # 空白画布必须给 theme（工具不带默认样式）；这里给一套中性的黑白
    style = {
        "font": {"latin": "Times New Roman", "east_asian": "宋体"},
        "colors": {"primary": "1F3864", "secondary": "2B2D42", "light": "D9E2F3",
                   "accent": "C00000", "white": "FFFFFF"},
        "body_sizes": [[4, 24], [6, 20], [99, 18]],
    }
    output = f"{topic}.pptx"
    receipt = pptx(file=output, slides=slides, style=style, overwrite=True, base_dir=str(workspace))

    return OfflineResult(
        preset="pptx_deck",
        files=[str(workspace / output)],
        summary=f"已生成 {output}（{len(slides)} 页，素材：{material.name}）\n{receipt.splitlines()[0]}",
        notes=[
            "离线模式：每页标题取自素材的章节标题、要点是**原文摘录**，没有改写或提炼。"
            f"要求 {wanted} 页，实际按素材章节数出 {len(slides)} 页。",
        ],
    )


# ---------------------------------------------------------------- 分发

RUNNERS = {
    "chart_png": run_chart,
    "table_xlsx": run_table,
    "archive_files": run_archive,
    "report_docx": run_report,
    "pptx_deck": run_deck,
}


def run_preset(preset_id: str, values: dict[str, str], workspace: Path) -> OfflineResult:
    """按预设 id 走对应的离线流程。"""
    runner = RUNNERS.get(preset_id)
    if runner is None:
        raise RuntimeError(f"这个预设没有离线实现：{preset_id}（可用：{'、'.join(sorted(RUNNERS))}）")
    return runner(values, workspace)
