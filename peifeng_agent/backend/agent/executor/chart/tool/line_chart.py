"""line_chart 工具：画折线图，适合展示随时间的趋势或连续变化。

蓝白科研风：白底、深蓝系配色、浅灰虚线网格、去上右边框、300 dpi 输出。
数据可以直接给（categories + series），也可以从 .xlsx / .csv 按列名取（source）。

工具约定：文件头 information 元数据 + 与 name 同名的业务函数；成功 return 文本，失败抛 RuntimeError。
"""
from __future__ import annotations

#: 一次最多处理几个目标
MAX_BATCH = 0  #: 0 = 不限条数（输出量另有上限保护）

from peifeng_agent.backend.utils.text import as_dict_list, call_many

from typing import Optional

from peifeng_agent.backend.utils import chart_kit as kit

information = {
    "name": "line_chart",
    "description": (
        "画折线图（PNG）：展示随时间或连续变量的变化趋势。"
        "用于：看指标随时间怎么变、比较多条曲线的走势时。"
        "数据可直接给 categories + series=[{name, values}]，"
        "也可用 source={file, sheet?, category, values:[列名]} 从 .xlsx/.csv 按列名取。"
        "蓝白科研风配色、中文字体、300 dpi 已内置；"
        "返回出图回执（标题、轴标签、分类、系列范围、实际像素、中文字体）。相对路径以 <项目目录> 为基准"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {"type": "string", "description": "输出图片路径（.png，放 <项目目录> 下）"},
            "title": {"type": "string", "description": "标题，写结论而不是图名（如 三季度产值回落 17%）"},
            "xlabel": {"type": "string", "description": "x 轴标签（如 月份）"},
            "ylabel": {"type": "string", "description": "y 轴标签，带单位（如 产值（万元））"},
            "categories": {"type": "array", "description": "x 轴分类标签，如 [1月, 2月, 3月]"},
            "series": {
                "type": "array",
                "description": "数据系列 [{name, values}]，values 与 categories 等长",
            },
            "style": {
                "type": "object",
                "description": (
                    "样式覆盖（可选，来自技能的 style 段）：font/font_latin/font_size/title_size/"
                    "tick_size/legend_size/palette/text/grid_color/spine/background/show_grid/"
                    "grid_axis/title_position(top|bottom)/legend(auto|none)/dpi。不传用默认蓝白科研风"
                ),
            },
            "source": {
                "type": "object",
                "description": "从表格取数：{file, sheet?, category, values:[列名]}；给了它可省略 categories/series",
            },
            "markers": {"type": "boolean", "description": "是否画数据点标记（默认 true）"},
        },
        "required": [],
    },
}


information["parameters"]["properties"]["charts"] = {
    "type": "array",
    "description": (
        "一次处理多个目标（与 output 互斥，）：每项是对象，"
        "键与单次调用的参数同名（如 {output: ...}）。要产出多个时用这个，不要一个一次调用"
    ),
    "items": {
        "type": "object",
        # 元素字段 = 单次调用的全部参数（只留类型，不复制描述，控制体积）；
        # 只声明文件名会让模型以为不能传 blocks/slides，生成出空文档
        "properties": {
            key: {"type": spec.get("type", "string")}
            for key, spec in information["parameters"]["properties"].items()
            if key != "charts"
        },
        "required": ["output"],
    },
}


def line_chart(
    output: str = "",
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    categories: Optional[list] = None,
    series: Optional[list] = None,
    source: Optional[dict] = None,
    style: Optional[dict] = None,
    markers: bool = True,
    charts: list = None,
    base_dir=None,
) -> str:
    """画折线并保存为 PNG，返回落盘结果；相对路径以 base_dir（项目工作文件夹）为基准。"""
    batch = as_dict_list(charts, "charts", MAX_BATCH)
    if batch:
        return call_many(line_chart, batch, "charts", base_dir=base_dir, preview="output")
    if not str(output or "").strip():
        raise RuntimeError("line_chart 需要 output（单个）或 charts（多个）之一")
    target = kit.prepare(output, base_dir)

    if isinstance(source, dict) and source:
        labels, data = kit.load_table(source, base_dir)
    else:
        labels = [str(item) for item in (categories or [])]
        data = kit.clean_series(series)
    kit.check_lengths(labels, data)

    spec = kit.apply(style)
    figure, axes = kit.plt.subplots(figsize=kit.figure_size(len(labels)))
    positions = kit.x_positions(labels)
    for index, item in enumerate(data):
        axes.plot(
            positions,
            item["values"],
            label=item["name"],
            color=kit.palette_of(spec)[index % len(kit.palette_of(spec))],
            marker="o" if markers else None,
            markersize=4,
            linewidth=1.8,
        )
    kit.label_x(axes, positions, labels)
    kit.clean_axes(axes, grid_axis="y", spec=spec)
    kit.place_legend(axes, len(data), spec=spec)
    if title:
        kit.put_title(figure, axes, title, spec, pad=kit.title_pad(axes, len(data)))
    if xlabel:
        axes.set_xlabel(xlabel)
    if ylabel:
        axes.set_ylabel(ylabel)
    figure.tight_layout()
    kit.save(figure, target, spec)
    return kit.report(target, kind="折线图", axes=axes, categories=labels, series=data)
