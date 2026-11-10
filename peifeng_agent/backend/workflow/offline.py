"""离线保底执行：不调用任何 LLM，按固定流程把预设任务做完。

**为什么能离线**：预设任务的流程本来就是确定的——「读表格 → 按列画图」「按扩展名归档」
这类活的步骤不依赖模型决策。工具层（chart_kit / docx / pptx / xlsx）本身也不调模型，
所以只要绕过 planner 与 worker 的对话循环，直接调工具就能跑。

**边界**：只保留**做得完整**的三个（读表画图、清洗汇总、按类型归档）——
它们不需要理解自然语言，规则是确定的。而"写文档""做 PPT"这类要生成内容的任务
离线只能重组素材原文，质量不达标，**不做离线版**，一律走在线模式。
"""
from __future__ import annotations

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

    if text_columns:
        # 有文本列：第一个文本列当分类（如「班级」「产品名」）
        category_index = text_columns[0]
        value_indexes = numeric
    else:
        # 全是数值列（实验数据常见：温度 × 转化率 × 选择性）：第一列当 X 轴，其余当系列
        if len(numeric) < 2:
            raise RuntimeError(
                f"{path.name} 只有一列数值，缺少分类列：要么加一列「名称/班级」，"
                "要么把 X 轴那列一起放进来（如温度、转化率、选择性）"
            )
        category_index = numeric[0]
        value_indexes = numeric[1:]

    labels: list[str] = []
    buckets: list[list[float]] = [[] for _ in value_indexes]
    for row in rows[1:]:
        cell = row[category_index] if category_index < len(row) else None
        if cell is None or not str(cell).strip():
            continue
        # 数值型分类（温度 300/350…）去掉多余小数：300.0 → 300
        number = to_number(cell)
        labels.append(f"{number:g}" if number is not None else str(cell).strip())
        for position, index in enumerate(value_indexes):
            buckets[position].append(to_number(row[index] if index < len(row) else None) or 0.0)
    if not labels:
        raise RuntimeError("分类列全是空值，无法出图")

    series = [{"name": header[index], "values": bucket} for index, bucket in zip(value_indexes, buckets)]
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


# ---------------------------------------------------------------- 公文排版

#: 党政机关公文格式（GB/T 9704-2012）常用参数。字号用磅值：二号=22、三号=16、四号=14。
OFFICIAL_DOC_FORMAT = {
    "page": {"top": 3.7, "bottom": 3.5, "left": 2.8, "right": 2.6},
    "styles": {
        "title": {"font": "方正小标宋简体", "font_east_asian": "方正小标宋简体", "size": 22, "align": "center"},
        "heading1": {"font": "黑体", "font_east_asian": "黑体", "size": 16},
        "heading2": {"font": "楷体", "font_east_asian": "楷体", "size": 16},
        "body": {"font": "仿宋", "font_east_asian": "仿宋", "size": 16, "line_spacing": 28},
        "signature": {"font": "仿宋", "font_east_asian": "仿宋", "size": 16, "align": "right"},
    },
}

#: 稿件约定：``# 标题``、``## 一级标题``、``## 二级标题``、其余行是正文
HEADING = re.compile(r"^(#{1,6})\s*(.+)$")


def parse_manuscript(text: str) -> list[tuple[str, str]]:
    """把稿件解析成 ``[(类型, 文本)]``：``title`` / ``heading1`` / ``heading2`` / ``body``。

    落款按约定识别：最后几行里，以「年 月 日」结尾的那行算日期、它上面一行算署名。
    """
    blocks: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        match = HEADING.match(stripped)
        if match:
            level = len(match.group(1))
            kind = "title" if level == 1 else ("heading1" if level == 2 else "heading2")
            blocks.append((kind, match.group(2).strip()))
            continue
        blocks.append(("body", stripped))
    return blocks


def run_official_docx(values: dict[str, str], workspace: Path) -> OfflineResult:
    """把按约定写好的稿件排成公文格式的 .docx（只排版，不改内容）。"""
    from peifeng_agent.backend.agent.executor.office_docs.tool.docx import docx

    name = (values.get("source") or "").strip()
    if not name:
        raise RuntimeError("official_docx 需要 source（内容稿件，.md 或 .txt）")
    path = (workspace / name) if not Path(name).is_absolute() else Path(name)
    if not path.is_file():
        raise RuntimeError(f"稿件不存在：{path}")

    blocks_in = parse_manuscript(path.read_text(encoding="utf-8"))
    if not blocks_in:
        raise RuntimeError(f"{path.name} 是空的")

    blocks: list[dict[str, Any]] = []
    for kind, content in blocks_in:
        if kind == "title":
            blocks.append({"type": "title", "text": content})
        elif kind in ("heading1", "heading2"):
            blocks.append({"type": "heading", "level": 1 if kind == "heading1" else 2, "text": content})
        else:
            # 落款：以「年…月…日」结尾的短行按右对齐处理
            if re.search(r"\d{4}\s*年.*?\d{1,2}\s*月.*?\d{1,2}\s*日", content) and len(content) <= 40:
                blocks.append({"type": "paragraph", "style": "signature", "text": content})
            else:
                blocks.append({"type": "paragraph", "text": content})

    output = f"{path.stem}_公文.docx"
    receipt = docx(file=output, blocks=blocks, style=OFFICIAL_DOC_FORMAT,
                   overwrite=True, base_dir=str(workspace))

    return OfflineResult(
        preset="official_docx",
        files=[str(workspace / output)],
        summary=(
            f"已按公文格式排版 {output}：标题 {sum(1 for k, _ in blocks_in if k == 'title')} 个、"
            f"标题层级 {sum(1 for k, _ in blocks_in if k.startswith('heading'))} 个、"
            f"正文 {sum(1 for k, _ in blocks_in if k == 'body')} 段"
        ),
        notes=[
            "只做排版：字体（小标宋/黑体/楷体/仿宋）、字号（二/三号）、行距 28 磅、页边距均按 GB/T 9704 设置。",
            "内容一个字都没改——要改内容请改稿件后重跑。",
        ],
    )


# ---------------------------------------------------------------- PPT 套版

#: 页面稿约定：``# 封面标题`` 后跟 ``> 副标题``；``## 页标题`` 后跟 ``- 要点``
COVER = re.compile(r"^#\s+(.+)$")
PAGE = re.compile(r"^##\s+(.+)$")
BULLET = re.compile(r"^[-*]\s+(.+)$")
SUB = re.compile(r"^>\s*(.+)$")


def parse_pages(text: str) -> tuple[str, str, list[tuple[str, list[str]]]]:
    """把页面稿解析成 ``(封面标题, 副标题, [(页标题, [要点])])``。"""
    cover_title = ""
    subtitle = ""
    pages: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        match_cover = COVER.match(stripped)
        if match_cover and not cover_title and not pages:
            cover_title = match_cover.group(1).strip()
            continue
        match_page = PAGE.match(stripped)
        if match_page:
            current = []
            pages.append((match_page.group(1).strip(), current))
            continue
        match_sub = SUB.match(stripped)
        if match_sub and not subtitle and not pages:
            subtitle = match_sub.group(1).strip()
            continue
        match_bullet = BULLET.match(stripped)
        if match_bullet and current is not None:
            current.append(match_bullet.group(1).strip())
            continue
        if current is not None:
            current.append(stripped)
    return cover_title, subtitle, pages


def run_ppt_pages(values: dict[str, str], workspace: Path) -> OfflineResult:
    """把按约定写好的页面稿套成 .pptx（只排版，不改文字）。"""
    from peifeng_agent.backend.agent.executor.office_docs.tool.pptx import pptx

    name = (values.get("source") or "").strip()
    if not name:
        raise RuntimeError("ppt_pages 需要 source（页面稿，.md 或 .txt）")
    path = (workspace / name) if not Path(name).is_absolute() else Path(name)
    if not path.is_file():
        raise RuntimeError(f"页面稿不存在：{path}")

    cover, subtitle, pages = parse_pages(path.read_text(encoding="utf-8"))
    if not cover and not pages:
        raise RuntimeError(f"{path.name} 里没有识别到页面（用「# 封面标题」「## 页标题」开头）")

    slides: list[dict[str, Any]] = []
    if cover:
        slides.append({"type": "cover", "title": cover, "subtitle": subtitle})
    for title, bullets in pages:
        slides.append({"type": "content", "title": title, "bullets": bullets or ["（本页无要点）"]})

    style = {
        "font": {"latin": "Times New Roman", "east_asian": "宋体"},
        "colors": {"primary": "1F3864", "secondary": "2B2D42", "light": "D9E2F3",
                   "accent": "C00000", "white": "FFFFFF"},
        "body_sizes": [[4, 24], [6, 20], [99, 18]],
    }
    output = f"{path.stem}_演示稿.pptx"
    receipt = pptx(file=output, slides=slides, style=style, overwrite=True, base_dir=str(workspace))

    return OfflineResult(
        preset="ppt_pages",
        files=[str(workspace / output)],
        summary=f"已生成 {output}：{len(slides)} 页（封面 {1 if cover else 0} + 内容页 {len(pages)}）",
        notes=[
            "只做排版：每页标题与要点都取自页面稿原文，一个字没改。",
            "图、表、备注这类页面稿表达不了的内容，需要在线模式或手动补。",
        ],
    )


# ---------------------------------------------------------------- 分发

RUNNERS = {
    "official_docx": run_official_docx,
    "ppt_pages": run_ppt_pages,
    "chart_png": run_chart,
    "table_xlsx": run_table,
    "archive_files": run_archive,
}


def run_preset(preset_id: str, values: dict[str, str], workspace: Path) -> OfflineResult:
    """按预设 id 走对应的离线流程。"""
    runner = RUNNERS.get(preset_id)
    if runner is None:
        raise RuntimeError(f"这个预设没有离线实现：{preset_id}（可用：{'、'.join(sorted(RUNNERS))}）")
    return runner(values, workspace)
