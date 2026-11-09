"""图表工具包：四个画图工具共用的样式、取数与保存逻辑。

风格是蓝白科研风：白底、深蓝主色加蓝色渐变阶梯、浅灰虚线网格、去掉上右边框、
300 dpi 输出、无边框图例。四个工具只负责各自的画法，公共部分都在这里。

工具模块是按文件路径动态加载的，不能用相对导入，所以这里用绝对包路径被它们引用。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

import matplotlib

#: 无 GUI 环境必须用 Agg 后端（必须在 pyplot 之前设置）
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402 - 依赖上面的后端设置
from matplotlib.ticker import MaxNLocator  # noqa: E402

#: 中文字体候选（Windows 用微软雅黑，Linux/macOS 回落 Noto/DejaVu）
FONT_CANDIDATES = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans"]

#: 蓝白科研风配色：深蓝为主，逐级变浅，最后一个中灰用于对比
PALETTE = ("#1F4E79", "#2E75B6", "#5B9BD5", "#9DC3E6", "#BDD7EE", "#7F7F7F")

#: 文字、网格与坐标轴颜色
TEXT = "#262626"
GRID = "#D9D9D9"
SPINE = "#BFBFBF"

#: 出版级输出精度
DPI = 300

#: 一次最多画的分类数（再多图就不可读）
MAX_CATEGORIES = 60

#: 样式默认值：调用方给的 ``style`` 只覆盖这里出现的键，其余保持默认（蓝白科研风）。
#: 图表格式由技能决定——技能里的 style 段原样传进来即可，工具不替你做审美决定。
STYLE_DEFAULTS: dict[str, Any] = {
    "font": None,               # 中文字体名（如 "SimSun" 宋体）；None = 按 FONT_CANDIDATES 回落
    "font_latin": None,         # 西文/数字字体（如 "Times New Roman"）；None = 与中文相同
    "font_size": 10,            # 正文字号
    "title_size": 11,           # 标题字号
    "tick_size": 9,             # 刻度字号
    "legend_size": 9,           # 图例字号
    "palette": list(PALETTE),   # 系列配色（按顺序取用）
    "text": TEXT,               # 文字颜色
    "grid_color": GRID,         # 网格线颜色
    "spine": SPINE,             # 坐标轴颜色
    "background": "white",      # 画布底色
    "show_grid": True,          # 是否画网格
    "grid_axis": "y",           # 网格方向：x / y / both
    "title_position": "top",    # 图题位置：top（常用）/ bottom（论文里图题在图下）
    "legend": "auto",           # auto（有图例就画）/ none（不画图例，靠图注说明）
    "dpi": DPI,                 # 输出精度
}


def resolve_style(style: Optional[dict]) -> dict[str, Any]:
    """把调用方给的 style 合到默认值上（只认默认表里已有的键，非法值忽略）。"""
    spec = dict(STYLE_DEFAULTS)
    spec["palette"] = list(STYLE_DEFAULTS["palette"])
    if not isinstance(style, dict):
        return spec
    for key, value in style.items():
        if key in spec and value is not None:
            spec[key] = list(value) if key == "palette" and isinstance(value, (list, tuple)) else value
    return spec


def apply(style: Optional[dict] = None) -> dict[str, Any]:
    """统一字体、网格与坐标轴风格（每次画图前调用一次），返回生效的样式表。

    ``style`` 来自技能：字体、字号、配色、网格、图题位置、dpi 都能改，
    不传就用默认的蓝白科研风。返回的这份供各工具查 ``title_position`` / ``legend`` 等开关。
    """
    spec = resolve_style(style)
    fonts = list(FONT_CANDIDATES)
    if spec.get("font"):
        fonts = [str(spec["font"]), *FONT_CANDIDATES]
    plt.rcParams.update(
        {
            "font.family": "sans-serif" if not spec.get("font") else [str(spec["font"]), "sans-serif"],
            "font.sans-serif": fonts,
            "font.size": spec["font_size"],
            "axes.unicode_minus": False,
            "axes.edgecolor": spec["spine"],
            "axes.linewidth": 0.8,
            "axes.labelcolor": spec["text"],
            "axes.labelsize": spec["font_size"],
            "axes.titlesize": spec["title_size"],
            "axes.titlecolor": spec["text"],
            "axes.titlepad": 10,
            "axes.grid": bool(spec["show_grid"]),
            "axes.grid.axis": str(spec["grid_axis"]),
            "grid.color": spec["grid_color"],
            "grid.linestyle": "--",
            "grid.linewidth": 0.6,
            "xtick.color": spec["text"],
            "ytick.color": spec["text"],
            "xtick.labelsize": spec["tick_size"],
            "ytick.labelsize": spec["tick_size"],
            "legend.frameon": False,
            "legend.fontsize": spec["legend_size"],
            "figure.facecolor": spec["background"],
            "savefig.facecolor": spec["background"],
        }
    )
    return spec


def palette_of(spec: dict) -> list:
    """取系列配色；技能给的列表太短时循环取用。"""
    colors = list(spec.get("palette") or PALETTE)
    return colors or list(PALETTE)


def resolve(path: str, base_dir) -> Path:
    """相对路径以项目目录为基准（不依赖进程 cwd）。"""
    target = Path(path)
    if target.is_absolute() or base_dir is None:
        return target
    return Path(base_dir) / target


def prepare(output: str, base_dir) -> Path:
    """校验输出路径并返回绝对路径（只支持 PNG）。"""
    name = (output or "").strip()
    if not name:
        raise RuntimeError("output 不能为空（给一个 .png 文件名）")
    target = resolve(name, base_dir)
    if target.suffix.lower() != ".png":
        raise RuntimeError(f"只支持 PNG 输出（当前：{target.name}）")
    return target


def save(figure, target: Path, spec: Optional[dict] = None) -> None:
    """按 style 指定的精度与底色保存，并关闭画布。"""
    spec = spec or STYLE_DEFAULTS
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        target,
        dpi=int(spec.get("dpi") or DPI),
        bbox_inches="tight",
        facecolor=str(spec.get("background") or "white"),
    )
    plt.close(figure)


def pixel_size(target: Path) -> tuple[int, int]:
    """落盘图片的真实像素尺寸（保存时按 tight 裁剪，画布尺寸不等于最终尺寸）。"""
    try:
        from PIL import Image

        with Image.open(target) as image:
            return image.size
    except Exception:  # noqa: BLE001 - 读不到就退回画布估算
        return (0, 0)


def active_font(spec: Optional[dict] = None) -> str:
    """实际生效的中文字体名（渲染中文靠它，缺字形会变成方框）。"""
    from matplotlib import font_manager

    candidates = list(FONT_CANDIDATES)
    if spec and spec.get("font"):
        candidates = [str(spec["font"]), *candidates]
    for name in candidates:
        try:
            found = font_manager.findfont(font_manager.FontProperties(family=name), fallback_to_default=False)
        except Exception:  # noqa: BLE001 - 找不到就试下一个
            continue
        if found and "DejaVu" not in Path(found).name or name == "DejaVu Sans":
            return f"{name}（{Path(found).name}）"
    return "未知"


def report(target: Path, *, kind: str, axes, categories=None, series=None, notes: str = "") -> str:
    """出图回执：把"画对了没有"该核对的项目一次报全。

    标题、轴标签、分类数、系列名与取值范围、像素尺寸、字节数、实际中文字体——
    这些正是核验一张图是否可用要看的东西，一并给出就不必再解析像素去猜。
    """
    width, height = pixel_size(target)
    lines = [f"已生成{kind}：{target}（{width}×{height} 像素，{target.stat().st_size} 字节）"]
    title = axes.get_title()
    lines.append(f"标题：{title or '（未设）'}")
    xlabel, ylabel = axes.get_xlabel(), axes.get_ylabel()
    if xlabel or ylabel:
        lines.append(f"轴标签：x={xlabel or '（未设）'}  y={ylabel or '（未设）'}")
    if categories:
        shown = "、".join(str(item) for item in list(categories)[:8])
        more = f" 等 {len(categories)} 项" if len(categories) > 8 else f"（{len(categories)} 项）"
        lines.append(f"分类：{shown}{more}")
    if series:
        parts = []
        for item in series:
            values = [value for value in item.get("values", []) if value is not None]
            if values:
                parts.append(f"{item.get('name') or '系列'}（{min(values):g}–{max(values):g}）")
            else:
                parts.append(str(item.get("name") or "系列"))
        lines.append(f"系列：{'；'.join(parts)}")
    if notes:
        lines.append(notes)
    lines.append(f"中文渲染字体：{active_font()}")
    return "\n".join(lines)


def clean_axes(axes, *, grid_axis: str = "y", spec: Optional[dict] = None) -> None:
    """去掉上右边框、按 style 决定要不要网格，并把网格压到数据下方。

    网格**不写死 True**：``style.show_grid = false``（论文常见要求）时要真的关掉——
    这里曾经硬编码 True，rcParams 里的设置会被它覆盖。
    """
    spec = spec or STYLE_DEFAULTS
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    axes.spines["left"].set_color(spec.get("spine") or SPINE)
    axes.spines["bottom"].set_color(spec.get("spine") or SPINE)
    # 注意：grid(False, linestyle=...) 会**仍然启用网格**（matplotlib 认为给了线样式就是要画），
    # 所以关网格必须单独调 grid(False)，不能带样式参数
    if bool(spec.get("show_grid", True)):
        axes.grid(
            True,
            axis=str(spec.get("grid_axis") or grid_axis),
            linestyle="--",
            linewidth=0.6,
            color=spec.get("grid_color") or GRID,
        )
    else:
        axes.grid(False)
    axes.set_axisbelow(True)
    if grid_axis == "y":
        axes.yaxis.set_major_locator(MaxNLocator(nbins=6))
    else:
        axes.xaxis.set_major_locator(MaxNLocator(nbins=6))


def figure_size(count: int, *, ratio: float = 1.5, horizontal: bool = False) -> tuple[float, float]:
    """按分类数给一个合适的画布尺寸。"""
    width = min(11.0, max(5.6, count * 0.62))
    height = max(3.6, count * 0.34) if horizontal else width / ratio
    return (width, height)


def x_positions(labels: list[str]) -> list[int]:
    """分类的整数位置，分类多时标签斜排避免重叠。"""
    return list(range(len(labels)))


def label_x(axes, positions: list[int], labels: list[str]) -> None:
    """把分类标签贴到 x 轴，超过 12 个斜排。"""
    rotate = 0 if len(labels) <= 12 else 30
    axes.set_xticks(positions)
    axes.set_xticklabels(labels, rotation=rotate, ha="right" if rotate else "center")


def label_y(axes, positions: list[int], labels: list[str]) -> None:
    """把分类标签贴到 y 轴（条形图）。"""
    axes.set_yticks(positions)
    axes.set_yticklabels(labels)


def place_legend(axes, count: int, *, columns: int = 4, spec: Optional[dict] = None) -> None:
    """多系列时把图例横排到绘图区上方，避免压住数据（科研风常见做法）。

    ``spec["legend"] == "none"`` 时不画图例——论文里常把系列说明写进图注，图上不放图例。
    """
    if spec and str(spec.get("legend") or "auto").lower() == "none":
        return
    if count <= 1:
        return
    axes.legend(
        loc="lower left",
        bbox_to_anchor=(0, 1.02),
        ncol=min(count, columns),
        frameon=False,
        borderaxespad=0,
        handlelength=1.6,
        columnspacing=1.4,
    )


def put_title(figure, axes, title: str, spec: Optional[dict] = None, pad: int = 10) -> None:
    """按 ``style.title_position`` 放图题。

    ``top``（默认）用轴标题；``bottom`` 放在画布下方——中文论文/期刊常要求"图题在图下"，
    这时候用轴标题就不合规范了。
    """
    if not title:
        return
    spec = spec or STYLE_DEFAULTS
    if str(spec.get("title_position") or "top").lower() == "bottom":
        figure.text(
            0.5,
            -0.02,
            title,
            ha="center",
            va="top",
            fontsize=spec.get("title_size") or 11,
            color=spec.get("text") or TEXT,
        )
    else:
        axes.set_title(title, pad=pad)


def title_pad(axes, count: int) -> int:
    """有图例时把标题抬高，留出图例的位置。"""
    return 26 if count > 1 else 10


def numbers(values: Any, name: str) -> list[float]:
    """把一组值转成浮点，失败时给出可定位的错误。"""
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"{name} 需要是非空数组")
    try:
        return [float(value) for value in values]
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} 含非数字：{values!r}") from None


def clean_series(series: Optional[list]) -> list[dict]:
    """校验 series=[{name, values}]，转成 [{name, values: [float]}]。"""
    cleaned: list[dict] = []
    for index, item in enumerate(series or []):
        if not isinstance(item, dict):
            raise RuntimeError(f"series[{index}] 需要是对象 {{name, values}}")
        values = numbers(item.get("values"), f"series[{index}].values")
        cleaned.append({"name": str(item.get("name") or f"系列{index + 1}"), "values": values})
    return cleaned


def check_lengths(labels: list[str], data: list[dict]) -> None:
    """分类与每个系列必须等长，且分类数不超上限。"""
    if not labels:
        raise RuntimeError("没有分类标签：请给 categories，或用 source 指定表格里的分类列")
    if not data:
        raise RuntimeError("没有数据：请给 series=[{{name, values}}]，或用 source 指定表格里的数值列")
    if len(labels) > MAX_CATEGORIES:
        raise RuntimeError(f"分类过多（{len(labels)} 个，上限 {MAX_CATEGORIES}）：先聚合再画")
    for item in data:
        if len(item["values"]) != len(labels):
            raise RuntimeError(
                f"系列 {item['name']} 有 {len(item['values'])} 个值，但分类有 {len(labels)} 个：两者必须等长"
            )


def load_table(source: dict, base_dir, *, category: str = "category", values: str = "values"):
    """从 .xlsx / .csv 按列名取数，返回 ``(分类标签, [{name, values}])``。

    ``source`` 形如 ``{file, sheet?, category: 列名, values: [列名]}``；
    饼图与散点图用单列时传 ``values`` 为长度为 1 的列表即可。
    """
    raw = str(source.get("file") or "").strip()
    if not raw:
        raise RuntimeError("source.file 不能为空")
    path = resolve(raw, base_dir)
    if not path.is_file():
        raise RuntimeError(f"表格不存在：{path}")
    rows = read_table(path, str(source.get("sheet") or ""))
    if not rows:
        raise RuntimeError(f"表格没有数据：{path}")

    header = [str(cell).strip() for cell in rows[0]]
    body = rows[1:]
    category_column = str(source.get(category) or "").strip()
    if category_column not in header:
        raise RuntimeError(f"找不到分类列 {category_column}（表头：{'、'.join(header)}）")
    value_columns = [str(item).strip() for item in (source.get(values) or []) if str(item).strip()]
    if not value_columns:
        raise RuntimeError(f"source.{values} 不能为空（给要画的列名）")
    missing = [column for column in value_columns if column not in header]
    if missing:
        raise RuntimeError(f"找不到数值列：{'、'.join(missing)}（表头：{'、'.join(header)}）")

    category_index = header.index(category_column)
    labels: list[str] = []
    buckets: list[list[float]] = [[] for _ in value_columns]
    for row in body:
        cell = row[category_index] if category_index < len(row) else None
        if cell is None or not str(cell).strip():
            continue
        labels.append(str(cell).strip())
        for position, column in enumerate(value_columns):
            index = header.index(column)
            buckets[position].append(cell_number(row[index] if index < len(row) else None, column, labels[-1]))
    data = [{"name": column, "values": bucket} for column, bucket in zip(value_columns, buckets)]
    return labels, data


def read_table(path: Path, sheet: str = "") -> list[list]:
    """读取表格为二维列表：.xlsx 用 openpyxl（指定 sheet，缺省第一个），.csv 用标准库。"""
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [row for row in csv.reader(stream)]
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), read_only=True, data_only=True)
        try:
            worksheet = workbook[sheet] if sheet and sheet in workbook.sheetnames else workbook.worksheets[0]
            return [list(row) for row in worksheet.iter_rows(values_only=True)]
        finally:
            workbook.close()
    raise RuntimeError(f"只支持 .xlsx 或 .csv 取数（当前：{path.name}）")


def cell_number(cell, column: str, label: str) -> float:
    """把单元格转成数字；空值按 0，非数字给出可定位的错误。"""
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return 0.0
    try:
        return float(cell)
    except (TypeError, ValueError):
        raise RuntimeError(f"列 {column} 在 {label} 这一行的值不是数字：{cell!r}") from None
